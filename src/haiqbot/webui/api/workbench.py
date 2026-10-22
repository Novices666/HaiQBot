"""命令工作台（**只预演，不真发、不触发插件**）。

它回答的是"我发这句话，谁会接管、参数被解析成什么、为什么别人没接管"。
刻意**不调用** `rule.invoker` —— 一旦调用就等于真的执行了 handler，
命令工作台就变成了"远程执行面板"，那是另一个性质的东西。

返回里把「没匹配上的原因」一起给出来，因为排障时最有用的就是这一半信息。
"""

from __future__ import annotations

from typing import Any

from haiqbot.core.channel import Actor, ChannelId, ChannelKind, Role
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.segment import Segment
from haiqbot.webui.api.common import Ctx, fail, ok, read_json

_ROLE_NAMES = {1: "USER", 2: "ADMIN", 3: "OWNER"}


def _role(value: Any) -> Role:
    """Role 是 IntEnum：USER=1 / ADMIN=2 / OWNER=3（不是 0 起）。"""
    try:
        return Role(int(value))
    except (TypeError, ValueError):
        return Role.USER


def _channel_kind(value: Any) -> ChannelKind:
    try:
        return ChannelKind(str(value or "group"))
    except ValueError:
        return ChannelKind.GROUP


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime

    def _rules() -> list[Any]:
        bus = getattr(runtime, "bus", None)
        rules = getattr(bus, "_rules", None)
        if rules:
            return list(rules)
        out: list[Any] = []
        for handle in ctx.registry.handles().values() if ctx.registry else []:
            out.extend(handle.rules)
        return out

    async def parse(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))

        text = str(body.get("text") or "")
        if not text:
            return fail(400, "bad_request", "text 不能为空")

        platform = str(body.get("platform") or "onebot")
        kind = _channel_kind(body.get("channel_kind"))
        channel_id = str(body.get("channel_id") or "0")
        role = _role(body.get("role", 0))
        at_bot = bool(body.get("at_bot", True))

        event = MessageEvent(
            kind=EventKind.MESSAGE,
            channel=ChannelId(platform, kind, channel_id),
            actor=Actor(id=str(body.get("actor") or "webui-probe"), name="工作台预演", role=role),
            segments=[Segment.text(text)],
            msg_id="webui-dryrun",
            at_bot=at_bot,
            bot_id=None,
            window_ttl=None,
            raw={"dry_run": True},
        )

        matched: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for rule in sorted(_rules(), key=lambda r: (-r.priority, r.plugin_id)):
            reason = _skip_reason(rule, event, role)
            if reason:
                skipped.append(
                    {
                        "plugin": rule.plugin_id,
                        "rule": rule.label,
                        "priority": rule.priority,
                        "reason": reason,
                    }
                )
                continue
            outcome = rule.matches(event)
            if not outcome.matched:
                skipped.append(
                    {
                        "plugin": rule.plugin_id,
                        "rule": rule.label,
                        "priority": rule.priority,
                        "reason": "命令词/参数不匹配",
                    }
                )
                continue
            try:
                params = rule.build_kwargs(outcome, text)
            except Exception as exc:  # noqa: BLE001 - 参数解析失败本身就是要看的信息
                params = {"__error__": f"{type(exc).__name__}: {exc}"}
            matched.append(
                {
                    "plugin": rule.plugin_id,
                    "rule": rule.label,
                    "priority": rule.priority,
                    "commands": list(getattr(rule, "commands", ()) or ()),
                    "usage": getattr(rule, "usage", ""),
                    "block": bool(getattr(rule, "block", False)),
                    "params": {k: repr(v) for k, v in (params or {}).items() if k != "ctx"},
                }
            )

        return ok(
            {
                "dry_run": True,
                "note": "只做到解析与匹配，不调用插件、不发消息",
                "event": {
                    "text": text,
                    "platform": platform,
                    "channel_kind": str(kind),
                    "channel_id": channel_id,
                    "role": _ROLE_NAMES.get(int(role), str(role)),
                    "at_bot": at_bot,
                },
                "matched": matched,
                "skipped": skipped,
                "rules_total": len(_rules()),
            }
        )

    def _skip_reason(rule: Any, event: MessageEvent, role: Role) -> str | None:
        events = getattr(rule, "events", None)
        if events is not None and event.kind not in events:
            return "事件类型不匹配"
        platforms = getattr(rule, "platforms", None)
        if platforms is not None and event.channel.platform not in platforms:
            return f"平台不匹配（只接受 {', '.join(sorted(platforms))}）"
        kinds = getattr(rule, "channel_kinds", None)
        if kinds is not None and event.channel.kind not in kinds:
            return f"频道类型不匹配（只接受 {', '.join(k.value for k in kinds)}）"
        min_role = getattr(rule, "min_role", Role.USER)
        if role < min_role:
            need = _ROLE_NAMES.get(int(min_role), str(min_role))
            return f"权限不足（需要 {need}）"
        if getattr(rule, "invoker", None) is None:
            return "规则未绑定 handler（插件未加载完）"
        return None

    return [Route("/api/workbench/parse", parse, methods=["POST"])]
