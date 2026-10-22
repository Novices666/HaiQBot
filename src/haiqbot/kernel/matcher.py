"""声明式匹配器。

插件不写 if/else 路由，只声明「我关心什么事件」：

    @on_command("画图", aliases=["draw"], priority=10)

内核把它编译成一个 `MatchRule`，按 priority 从高到低依次尝试。
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from haiqbot.core.channel import ChannelKind, Role
from haiqbot.core.event import Event, EventKind, MessageEvent, NoticeEvent, RequestEvent

CommandFunc = Callable[..., Any]


@dataclass(slots=True)
class MatchOutcome:
    matched: bool
    args: list[str] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    match: re.Match[str] | None = None


@dataclass(slots=True)
class MatchRule:
    plugin_id: str
    func: CommandFunc
    events: frozenset[EventKind] = frozenset({EventKind.MESSAGE})
    platforms: frozenset[str] | None = None
    channel_kinds: frozenset[ChannelKind] | None = None
    min_role: Role = Role.USER
    # 功能级开关粒度：channel_config 里写 chat:ask 时，只有声明了 feature="ask" 的规则会跑
    feature: str | None = None
    commands: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    priority: int = 0
    block: bool = True
    require_at: bool = False
    usage: str | None = None
    description: str | None = None
    # 通知/请求的细分过滤：None = 不挑。留空字符串可以精确匹配"没有 sub_type"。
    notice_types: frozenset[str] | None = None
    notice_sub_types: frozenset[str] | None = None
    request_types: frozenset[str] | None = None
    request_sub_types: frozenset[str] | None = None
    # 该 handler 的独立超时；None 则沿用 Bus 的默认值
    timeout: float | None = None
    # 由注册器填充：把 (outcome) 转成 handler 调用参数
    _params: list[str] = field(default_factory=list)
    # 由注册器填充：把 (PipelineContext, MatchOutcome) 变成一次真实调用
    invoker: Any = None

    def __post_init__(self) -> None:
        self._params = _infer_params(self.func)

    # ------------------------------------------------------------------ #
    @property
    def label(self) -> str:
        if self.commands:
            return "/".join(self.commands)
        if self.pattern is not None:
            return f"re:{self.pattern.pattern}"
        return self.func.__name__

    def matches(self, event: Event) -> MatchOutcome:
        if event.kind not in self.events:
            return MatchOutcome(False)
        if self.platforms is not None and event.channel.platform not in self.platforms:
            return MatchOutcome(False)
        if self.channel_kinds is not None and event.channel.kind not in self.channel_kinds:
            return MatchOutcome(False)
        if event.actor.role < self.min_role:
            return MatchOutcome(False)

        # 通知/请求的细分过滤。放在命令匹配之前 —— 这类规则本来就不该有命令词。
        if self.notice_types is not None:
            if not isinstance(event, NoticeEvent) or event.notice_type not in self.notice_types:
                return MatchOutcome(False)
        if self.notice_sub_types is not None:
            if not isinstance(event, NoticeEvent) or event.sub_type not in self.notice_sub_types:
                return MatchOutcome(False)
        if self.request_types is not None:
            if not isinstance(event, RequestEvent) or event.request_type not in self.request_types:
                return MatchOutcome(False)
        if self.request_sub_types is not None:
            if not isinstance(event, RequestEvent) or event.sub_type not in self.request_sub_types:
                return MatchOutcome(False)

        if self.commands:
            if not isinstance(event, MessageEvent):
                return MatchOutcome(False)
            if self.require_at and not event.mentions_bot():
                return MatchOutcome(False)
            text = event.text.lstrip()
            head, _, rest = text.partition(" ")
            # 兼容 /cmd 与 cmd 两种写法
            head = head.lstrip("/")
            if head not in self.commands:
                return MatchOutcome(False)
            return MatchOutcome(True, args=rest.split() if rest else [])
        if self.pattern is not None:
            m = self.pattern.search(event.text)
            if not m:
                return MatchOutcome(False)
            return MatchOutcome(True, match=m)

        if self.require_at and isinstance(event, MessageEvent) and not event.mentions_bot():
            return MatchOutcome(False)
        return MatchOutcome(True)

    def build_kwargs(self, outcome: MatchOutcome, raw_text: str) -> dict[str, Any]:
        """把命令参数映射到 handler 的形参。最后一个形参吸收剩余全部文本。"""
        params = self._params
        if not params:
            return {}
        plain = [p for p in params if p != "match"]
        kwargs: dict[str, Any] = {}
        if outcome.match is not None:
            if "match" in params:
                kwargs["match"] = outcome.match
            if not plain:
                return kwargs

        tokens = outcome.args
        for idx, name in enumerate(plain):
            is_last = idx == len(plain) - 1
            if is_last:
                kwargs[name] = " ".join(tokens[idx:])
            elif idx < len(tokens):
                kwargs[name] = tokens[idx]
            else:
                break
        return kwargs


def _infer_params(func: CommandFunc) -> list[str]:
    """取 bound method 的形参名，跳过第一个（ctx）。"""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):  # pragma: no cover - 内建函数
        return []
    names = [
        name
        for name, param in sig.parameters.items()
        if param.kind in (param.POSITIONAL_OR_KEYWORD, param.KEYWORD_ONLY)
    ]
    return names[1:] if names else []
