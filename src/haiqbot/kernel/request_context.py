"""单次事件（一次请求）的上下文，用 contextvars 承载。

**为什么需要**：能力句柄（`ctx.image` / `ctx.llm`）是「每个插件一份、跨事件复用」的，
天生看不到「现在处理的是哪个群、谁在说话、trace 是什么」。以前只能靠参数一路传，
于是调用日志这类横切需求就无处落笔。

挂在 contextvar 上的好处：插件代码零改动，句柄内部就能读到当前事件。

用法（只在 `Bus.publish` 里做）::

    tokens = bind(event, event.trace_id)
    try:
        ...
    finally:
        unbind(tokens)
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

_current_event: contextvars.ContextVar[Any] = contextvars.ContextVar("haiqbot_event", default=None)
_current_trace: contextvars.ContextVar[str | None] = contextvars.ContextVar("haiqbot_trace", default=None)


@dataclass
class RequestState:
    """一次事件处理期间累计的状态。

    目前只用来数「这条消息到底触发了几个昂贵动作」——配额按动作计就靠它。
    """

    charges: int = 0


_current_state: contextvars.ContextVar[RequestState | None] = contextvars.ContextVar(
    "haiqbot_state", default=None
)

Tokens = tuple[
    contextvars.Token[Any],
    contextvars.Token[str | None],
    contextvars.Token[RequestState | None],
]


def bind(event: Any, trace_id: str | None = None) -> Tokens:
    """绑定当前事件。返回的 tokens 必须在 finally 里交给 `unbind`。"""
    return (
        _current_event.set(event),
        _current_trace.set(trace_id),
        _current_state.set(RequestState()),
    )


def unbind(tokens: Tokens) -> None:
    ev_token, trace_token, state_token = tokens
    _current_event.reset(ev_token)
    _current_trace.reset(trace_token)
    _current_state.reset(state_token)


def current_event() -> Any:
    return _current_event.get()


def current_trace() -> str | None:
    return _current_trace.get()


def current_state() -> RequestState | None:
    return _current_state.get()


def current_channel() -> str | None:
    channel = getattr(current_event(), "channel", None)
    return str(channel) if channel is not None else None


def current_actor() -> str | None:
    actor = getattr(current_event(), "actor", None)
    return getattr(actor, "id", None)
