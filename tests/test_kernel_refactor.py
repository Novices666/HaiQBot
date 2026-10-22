"""内核重构回归：活配置视图、请求上下文、总线可控放行。"""

from __future__ import annotations

from pathlib import Path

from haiqbot.adapters.mock import MockAdapter
from haiqbot.core.channel import Actor, ChannelId, ChannelKind
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.reply import Reply
from haiqbot.core.segment import Segment
from haiqbot.kernel import request_context
from haiqbot.kernel.bus import Bus
from haiqbot.kernel.matcher import MatchRule
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime


def _event(text: str = "/hi") -> MessageEvent:
    return MessageEvent(
        kind=EventKind.MESSAGE,
        channel=ChannelId("mock", ChannelKind.GROUP, "10086"),
        actor=Actor(id="u42"),
        segments=[Segment.text(text)],
        msg_id="m-refactor",
    )


def _reply() -> Reply:
    return Reply(MockAdapter(), ChannelId("mock", ChannelKind.GROUP, "10086"))


# -- 1. ctx.config 是活视图 ------------------------------------------------- #


async def test_live_config_shares_storage_with_runtime(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    handle = await registry.load("chat")
    # ① 运行期改配置 → 插件立刻看见（以前是拷贝，看不见）
    runtime.config.plugin_config.setdefault("chat", {})["ack_text"] = "改过了"
    assert handle.context.config["ack_text"] == "改过了"
    assert handle.plugin.config["ack_text"] == "改过了"
    # ② 插件自己写 → 回到同一份存储
    handle.context.config["probe"] = 7
    assert runtime.config.plugin_config["chat"]["probe"] == 7
    # ③ 删掉也同步
    del handle.context.config["probe"]
    assert "probe" not in runtime.config.plugin_config["chat"]


async def test_live_config_behaves_like_a_mapping(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    handle = await registry.load("chat")
    cfg = handle.context.config
    assert dict(cfg) == dict(runtime.config.plugin_config.get("chat", {}))
    assert "ack_text" in cfg or cfg.get("ack_text") is None
    assert isinstance(cfg.get("missing", "fallback"), str)
    assert len(cfg) == len(runtime.config.plugin_config.get("chat", {}))


# -- 2. 请求上下文 ---------------------------------------------------------- #


async def test_request_context_visible_inside_handler() -> None:
    bus = Bus()
    seen: dict[str, object] = {}
    rule = MatchRule(plugin_id="p", func=lambda ctx: None, commands=("hi",))

    async def invoker(pctx: object, outcome: object) -> bool:
        bus_ev = request_context.current_event()
        seen["channel"] = request_context.current_channel()
        seen["actor"] = request_context.current_actor()
        seen["trace"] = request_context.current_trace()
        seen["same_event"] = bus_ev is pctx.event  # type: ignore[attr-defined]
        return True

    rule.invoker = invoker
    bus.register(rule)
    event = _event()
    event.trace_id = "trace-xyz"
    await bus.publish(event, _reply())
    assert seen["channel"] == "mock:group:10086"
    assert seen["actor"] == "u42"
    assert seen["trace"] == "trace-xyz"
    assert seen["same_event"] is True


async def test_request_context_is_cleared_after_publish() -> None:
    bus = Bus()
    rule = MatchRule(plugin_id="p", func=lambda ctx: None, commands=("hi",))

    async def invoker(pctx: object, outcome: object) -> bool:
        return True

    rule.invoker = invoker
    bus.register(rule)
    await bus.publish(_event(), _reply())
    assert request_context.current_event() is None
    assert request_context.current_trace() is None


async def test_request_context_survives_nested_capability_calls() -> None:
    """能力句柄内部（跨一层协程）仍然读得到当前事件 —— 调用日志靠这个。"""
    bus = Bus()
    captured: dict[str, object] = {}

    async def fake_capability() -> None:
        captured["channel"] = request_context.current_channel()

    rule = MatchRule(plugin_id="p", func=lambda ctx: None, commands=("hi",))

    async def invoker(pctx: object, outcome: object) -> bool:
        await fake_capability()
        return True

    rule.invoker = invoker
    bus.register(rule)
    await bus.publish(_event(), _reply())
    assert captured["channel"] == "mock:group:10086"


# -- 3. 总线可控放行 -------------------------------------------------------- #


def _rule(pid: str, priority: int, block: bool = True) -> MatchRule:
    return MatchRule(plugin_id=pid, func=lambda ctx: None, commands=("hi",), priority=priority, block=block)


async def test_handler_returning_false_releases_to_next_rule() -> None:
    bus = Bus()
    calls: list[str] = []
    first = _rule("first", priority=10)

    async def first_invoker(pctx: object, outcome: object) -> bool:
        calls.append("first")
        return False  # 明确说「我不管」

    first.invoker = first_invoker
    second = _rule("second", priority=0)

    async def second_invoker(pctx: object, outcome: object) -> bool:
        calls.append("second")
        return True

    second.invoker = second_invoker
    bus.register(first)
    bus.register(second)
    pctx = await bus.publish(_event(), _reply())
    assert calls == ["first", "second"], "放行后后面的规则必须有机会"
    assert pctx.handled is True
    assert pctx.plugin_id == "second"


async def test_handler_returning_none_still_claims() -> None:
    """老插件（return None）行为不变：视为已承接，不再往下走。"""
    bus = Bus()
    calls: list[str] = []
    first = _rule("first", priority=10)

    async def first_invoker(pctx: object, outcome: object) -> bool:
        calls.append("first")
        return True

    first.invoker = first_invoker
    second = _rule("second", priority=0)

    async def second_invoker(pctx: object, outcome: object) -> bool:
        calls.append("second")
        return True

    second.invoker = second_invoker
    bus.register(first)
    bus.register(second)
    pctx = await bus.publish(_event(), _reply())
    assert calls == ["first"]
    assert pctx.handled is True


async def test_all_rules_declining_leaves_event_unhandled() -> None:
    bus = Bus()
    rule = _rule("only", priority=0)

    async def invoker(pctx: object, outcome: object) -> bool:
        return False

    rule.invoker = invoker
    bus.register(rule)
    pctx = await bus.publish(_event(), _reply())
    assert pctx.handled is False
    assert pctx.plugin_id == "only"


async def test_timeout_counts_as_consumed_not_declined() -> None:
    """超时/异常不能被当成「放行」，否则一个坏插件会让所有插件轮着跑一遍。"""
    import asyncio

    bus = Bus(timeout=0.05)
    calls: list[str] = []
    slow = _rule("slow", priority=10)

    async def slow_invoker(pctx: object, outcome: object) -> bool:
        calls.append("slow")
        await asyncio.sleep(1)
        return True

    slow.invoker = slow_invoker
    other = _rule("other", priority=0)

    async def other_invoker(pctx: object, outcome: object) -> bool:
        calls.append("other")
        return True

    other.invoker = other_invoker
    bus.register(slow)
    bus.register(other)
    pctx = await bus.publish(_event(), _reply())
    assert calls == ["slow"], "超时算已消费，不应继续派发"
    assert pctx.handled is False


# -- 4. 配额按动作计 -------------------------------------------------------- #
