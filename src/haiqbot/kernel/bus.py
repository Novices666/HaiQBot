"""异步事件总线。

固定顺序的中间件链 → 声明式分发 → 审计。任一环可短路。
插件卡死不会拖垮整条链：每个 handler 都有独立超时。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from haiqbot.core.errors import StopPipeline
from haiqbot.core.event import Event
from haiqbot.core.reply import Reply
from haiqbot.kernel import request_context
from haiqbot.kernel.log import get_logger
from haiqbot.kernel.matcher import MatchOutcome, MatchRule

log = get_logger(__name__)


def rule_in_scope(rule: MatchRule, extra: dict[str, Any]) -> bool:
    """频道开关是否允许这条规则跑。

    extra 由 _mw_scope 填写：
      scope_closed    本频道整体关闭（未配置 / enabled=0 / 开启项为空）
      allowed_plugins 本频道允许的项（painter 或 chat:ask）
      always_plugins  声明 gating = "always" 的插件，**不受频道开关约束**
    """
    always = extra.get("always_plugins") or set()
    if rule.plugin_id in always:
        return True
    if extra.get("scope_closed"):
        return False
    allowed = extra.get("allowed_plugins")
    if allowed is None:
        return True
    return rule_allowed(rule, allowed)


def rule_allowed(rule: MatchRule, allowed: set[str]) -> bool:
    """llowed 既可能是整插件（painter），也可能是单个功能（chat:ask）。

    插件在名单里 → 它的规则都跑；只有 plugin:feature → 仅声明了该 feature 的规则跑。
    """
    if rule.plugin_id in allowed:
        return True
    feature = getattr(rule, "feature", None)
    return bool(feature) and f"{rule.plugin_id}:{feature}" in allowed


Middleware = Callable[["PipelineContext"], Awaitable[None]]


@dataclass(slots=True)
class PipelineContext:
    """一次事件在管线里的全部状态。"""

    event: Event
    reply: Reply
    trace_id: str
    handled: bool = False
    stopped: bool = False
    stop_reason: str = ""
    handler_count: int = 0
    plugin_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def stop(self, reason: str) -> None:
        raise StopPipeline(reason)


class Bus:
    def __init__(self, *, timeout: float = 8.0, max_handlers: int = 1) -> None:
        self.timeout = timeout
        self.max_handlers = max_handlers
        self._middlewares: list[tuple[int, str, Middleware]] = []
        self._rules: list[MatchRule] = []

    # -- 装配 ------------------------------------------------------------- #
    def add_middleware(self, name: str, fn: Middleware, order: int = 500) -> None:
        self._middlewares.append((order, name, fn))
        self._middlewares.sort(key=lambda item: item[0])
        log.debug("middleware_added", name=name, order=order)

    def remove_middleware(self, name: str) -> None:
        self._middlewares = [m for m in self._middlewares if m[1] != name]

    def register(self, rule: MatchRule) -> None:
        self._rules.append(rule)
        log.debug("rule_registered", plugin=rule.plugin_id, rule=rule.label, priority=rule.priority)

    def unregister_plugin(self, plugin_id: str) -> int:
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.plugin_id != plugin_id]
        return before - len(self._rules)

    def rules_of(self, plugin_id: str) -> list[MatchRule]:
        return [r for r in self._rules if r.plugin_id == plugin_id]

    @property
    def middleware_names(self) -> list[str]:
        return [name for _, name, _ in self._middlewares]

    # -- 执行 ------------------------------------------------------------- #
    async def publish(self, event: Event, reply: Reply) -> PipelineContext:
        ctx = PipelineContext(event=event, reply=reply, trace_id=event.trace_id)
        structlog.contextvars.bind_contextvars(trace_id=event.trace_id)
        tokens = request_context.bind(event, event.trace_id)
        try:
            for _, name, middleware in self._middlewares:
                try:
                    await middleware(ctx)
                except StopPipeline as exc:
                    ctx.stopped = True
                    ctx.stop_reason = exc.reason
                    log.debug("pipeline_stopped", at=name, reason=exc.reason)
                    break
                if ctx.stopped:
                    break
            if not ctx.stopped:
                await self._dispatch(ctx)
        finally:
            state = request_context.current_state()
            if state is not None:
                ctx.extra["charges"] = state.charges
            request_context.unbind(tokens)
            structlog.contextvars.unbind_contextvars("trace_id")
        return ctx

    async def _dispatch(self, ctx: PipelineContext) -> None:
        candidates = sorted(self._rules, key=lambda r: (-r.priority, r.plugin_id))
        # 频道开关的判定集中在 rule_in_scope（含 always 豁免）
        for rule in candidates:
            if not rule_in_scope(rule, ctx.extra):
                continue
            if rule.invoker is None:
                continue
            outcome = rule.matches(ctx.event)
            if not outcome.matched:
                continue

            ctx.plugin_id = rule.plugin_id
            verdict = await self._invoke(rule, ctx, outcome)
            if verdict is False:
                # 匹配但放行：不算消费，不计入 handler_count，后面的规则仍有机会。
                # 这是「多个插件共存」的关键 —— 没有它就只能靠 priority 抢。
                log.debug("handler_declined", plugin=rule.plugin_id, rule=rule.label)
                continue
            if verdict is True:
                ctx.handled = True
            ctx.handler_count += 1
            if rule.block or ctx.handler_count >= self.max_handlers:
                return

    async def _invoke(self, rule: MatchRule, ctx: PipelineContext, outcome: MatchOutcome) -> bool | None:
        """True=已承接｜False=明确放行｜None=出错或超时（算消费，但没处理成）。"""
        limit = rule.timeout if rule.timeout else self.timeout
        try:
            async with asyncio.timeout(limit):
                assert rule.invoker is not None
                return bool(await rule.invoker(ctx, outcome))
        except TimeoutError:
            log.warning(
                "handler_timeout",
                plugin=rule.plugin_id,
                rule=rule.label,
                timeout=limit,
            )
        except StopPipeline as exc:
            ctx.stopped = True
            ctx.stop_reason = exc.reason
            log.debug("handler_stopped", plugin=rule.plugin_id, reason=exc.reason)
        except Exception:
            log.exception("handler_error", plugin=rule.plugin_id, rule=rule.label)
        return None
