"""事件总线：中间件顺序、短路、超时、优先级分发。"""

from __future__ import annotations

import asyncio

from haiqbot.adapters.mock import MockAdapter
from haiqbot.core.channel import Actor, ChannelId, ChannelKind
from haiqbot.core.errors import StopPipeline
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.reply import Reply
from haiqbot.core.segment import Segment
from haiqbot.kernel.bus import Bus
from haiqbot.kernel.matcher import MatchRule


def _event(text: str = "/hi") -> MessageEvent:
    return MessageEvent(
        kind=EventKind.MESSAGE,
        channel=ChannelId("mock", ChannelKind.GROUP, "1"),
        actor=Actor(id="u1"),
        segments=[Segment.text(text)],
        msg_id="m1",
    )


def _reply() -> Reply:
    adapter = MockAdapter()
    return Reply(adapter, ChannelId("mock", ChannelKind.GROUP, "1"))


async def _noop(ctx: object) -> None: ...


def _rule(
    pid: str, *, commands: tuple[str, ...] = ("hi",), priority: int = 0, block: bool = True
) -> MatchRule:
    return MatchRule(plugin_id=pid, func=_noop, commands=commands, priority=priority, block=block)


async def test_middleware_order_then_handler() -> None:
    bus = Bus()
    order: list[str] = []

    async def m1(ctx: object) -> None:
        order.append("m1")

    async def m2(ctx: object) -> None:
        order.append("m2")

    bus.add_middleware("m2", m2, order=20)
    bus.add_middleware("m1", m1, order=10)  # 故意乱序注册，验证按 order 排序

    rule = _rule("p")

    async def invoker(pctx: object, outcome: object) -> bool:
        order.append("handler")
        return True

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event(), _reply())
    assert order == ["m1", "m2", "handler"]
    assert ctx.handled is True
    assert bus.middleware_names == ["m1", "m2"]


async def test_middleware_stop_blocks_handler() -> None:
    bus = Bus()
    order: list[str] = []

    async def m1(ctx: object) -> None:
        order.append("m1")

    async def stopper(ctx: object) -> None:
        raise StopPipeline("nope")

    bus.add_middleware("m1", m1, order=10)
    bus.add_middleware("guard", stopper, order=20)

    rule = _rule("p")

    async def invoker(pctx: object, outcome: object) -> bool:
        order.append("handler")
        return True

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event(), _reply())
    assert order == ["m1"]
    assert ctx.stopped is True
    assert ctx.stop_reason == "nope"
    assert ctx.handled is False


async def test_handler_timeout_does_not_raise() -> None:
    bus = Bus(timeout=0.05)
    rule = _rule("slow")

    async def invoker(pctx: object, outcome: object) -> bool:
        await asyncio.sleep(1.0)
        return True

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event(), _reply())
    assert ctx.handled is False


async def test_handler_exception_is_contained() -> None:
    bus = Bus()
    rule = _rule("boom")

    async def invoker(pctx: object, outcome: object) -> bool:
        raise RuntimeError("boom")

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event(), _reply())
    assert ctx.handled is False
    assert ctx.stopped is False


async def test_priority_and_block() -> None:
    bus = Bus(max_handlers=5)
    seen: list[str] = []

    def make(pid: str, priority: int, block: bool) -> MatchRule:
        rule = _rule(pid, priority=priority, block=block)

        async def invoker(pctx: object, outcome: object, _pid: str = pid) -> bool:
            seen.append(_pid)
            return True

        rule.invoker = invoker
        return rule

    bus.register(make("low", 0, True))
    bus.register(make("high", 10, True))

    await bus.publish(_event(), _reply())
    assert seen == ["high"]  # 高优先级 block=True → 后续不再执行


async def test_non_matching_rule_skipped() -> None:
    bus = Bus()
    rule = _rule("p", commands=("other",))

    async def invoker(pctx: object, outcome: object) -> bool:
        raise AssertionError("不应被执行")

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event("/hi"), _reply())
    assert ctx.handled is False


async def test_unregister_plugin() -> None:
    bus = Bus()
    bus.register(_rule("a"))
    bus.register(_rule("b"))
    assert bus.unregister_plugin("a") == 1
    assert [r.plugin_id for r in bus.rules_of("b")] == ["b"]


async def test_per_rule_timeout_overrides_bus_default() -> None:
    """生图这类长任务必须能声明自己的超时，不能用 Bus 的默认 8s。"""
    bus = Bus(timeout=0.05)  # 默认极短，模拟"生图会被砍掉"的场景
    rule = MatchRule(plugin_id="slow", func=_noop, commands=("hi",), timeout=5.0)

    async def invoker(pctx: object, outcome: object) -> bool:
        await asyncio.sleep(0.2)
        return True

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event(), _reply())
    assert ctx.handled is True, "规则级 timeout 应当覆盖 Bus 默认超时"


async def test_rule_without_timeout_still_uses_bus_default() -> None:
    bus = Bus(timeout=0.05)
    rule = _rule("slow")

    async def invoker(pctx: object, outcome: object) -> bool:
        await asyncio.sleep(0.2)
        return True

    rule.invoker = invoker
    bus.register(rule)

    ctx = await bus.publish(_event(), _reply())
    assert ctx.handled is False, "未声明 timeout 时应回落到 Bus 默认值"


async def test_sync_push_callback_is_supported() -> None:
    """同步 push 不该在事件到达时才抛 await 错误。"""
    from haiqbot.adapters.mock import MockAdapter

    adapter = MockAdapter()
    seen: list[object] = []
    adapter.bind(seen.append)  # 同步函数
    from haiqbot.core.channel import Actor, ChannelId, ChannelKind
    from haiqbot.core.event import EventKind, MessageEvent
    from haiqbot.core.segment import Segment

    await adapter._emit(
        MessageEvent(
            kind=EventKind.MESSAGE,
            channel=ChannelId("mock", ChannelKind.GROUP, "1"),
            actor=Actor(id="u1"),
            segments=[Segment.text("hi")],
            msg_id="m1",
            at_bot=False,
            bot_id="b",
        )
    )
    assert len(seen) == 1
