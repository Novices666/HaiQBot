"""事件幂等：三种事件各自怎么算 key，中间件怎么用它拦重复。

背景：OneBot 上报端在上报超时会**重发同一事件**（http/sse 模式尤其明显）。
去重原来只覆盖消息；重复的加群/加好友申请会走到审批逻辑里，重复通知会重复欢迎。
"""

from __future__ import annotations

import pytest

from haiqbot.adapters.mock import MockAdapter
from haiqbot.app import App
from haiqbot.core.channel import Actor, ChannelId, ChannelKind
from haiqbot.core.errors import StopPipeline
from haiqbot.core.event import EventKind, MessageEvent, NoticeEvent, RequestEvent
from haiqbot.core.reply import Reply
from haiqbot.kernel.bus import PipelineContext


def _group() -> ChannelId:
    return ChannelId("onebot", ChannelKind.GROUP, "g1")


def _actor(uid: str = "u1") -> Actor:
    return Actor(id=uid)


# --------------------------------------------------------------------------- #
# key 怎么算
# --------------------------------------------------------------------------- #
def test_message_key_is_platform_and_msg_id() -> None:
    event = MessageEvent(kind=EventKind.MESSAGE, channel=_group(), actor=_actor(), msg_id="m1")
    assert event.dedupe_key() == "onebot:m1"
    blank = MessageEvent(kind=EventKind.MESSAGE, channel=_group(), actor=_actor(), msg_id="")
    assert blank.dedupe_key() == "", "没有 msg_id 就无法判重，不能拦"


def test_request_key_prefers_flag() -> None:
    """flag 是协议端发的审批凭据，天然唯一。"""
    first = RequestEvent(kind=EventKind.REQUEST, channel=_group(), actor=_actor(), flag="f1")
    again = RequestEvent(
        kind=EventKind.REQUEST, channel=_group(), actor=_actor(), flag="f1", raw={"time": 999}
    )
    assert first.dedupe_key() == again.dedupe_key()


def test_request_without_flag_falls_back_to_time() -> None:
    raw = {"time": 1700000000}
    a = RequestEvent(kind=EventKind.REQUEST, channel=_group(), actor=_actor(), request_type="group", raw=raw)
    b = RequestEvent(
        kind=EventKind.REQUEST, channel=_group(), actor=_actor(), request_type="group", raw=dict(raw)
    )
    assert a.dedupe_key() and a.dedupe_key() == b.dedupe_key()
    c = RequestEvent(
        kind=EventKind.REQUEST,
        channel=_group(),
        actor=_actor(),
        request_type="group",
        raw={"time": 1700000001},
    )
    assert c.dedupe_key() != a.dedupe_key(), "隔一秒的两次申请是两件事"


def test_request_without_flag_or_time_is_not_deduped() -> None:
    event = RequestEvent(kind=EventKind.REQUEST, channel=_group(), actor=_actor())
    assert event.dedupe_key() == ""


def test_notice_recall_uses_notice_id() -> None:
    """撤回的 notice_id 借的是 message_id：同一 msg_id 撤回只处理一次。"""
    event = NoticeEvent(
        kind=EventKind.NOTICE,
        channel=_group(),
        actor=_actor(),
        notice_type="group_recall",
        notice_id="m123",
    )
    assert event.dedupe_key() == "onebot:notice:group_recall:m123"


def test_poke_is_never_deduped() -> None:
    """连戳两下是正常操作，不能当重复上报吃掉。"""
    event = NoticeEvent(
        kind=EventKind.NOTICE,
        channel=_group(),
        actor=_actor(),
        notice_type="notify",
        sub_type="poke",
        raw={"time": 1700000000},
    )
    assert event.dedupe_key() == ""


def test_notice_without_time_is_not_deduped() -> None:
    event = NoticeEvent(kind=EventKind.NOTICE, channel=_group(), actor=_actor(), notice_type="group_increase")
    assert event.dedupe_key() == ""


def test_notice_key_distinguishes_seconds_and_people() -> None:
    base = dict(kind=EventKind.NOTICE, channel=_group(), actor=_actor("u1"), notice_type="group_increase")
    a = NoticeEvent(**base, raw={"time": 1700000000})
    b = NoticeEvent(**base, raw={"time": 1700000000})
    assert a.dedupe_key() == b.dedupe_key()
    c = NoticeEvent(**base, raw={"time": 1700000006})
    assert c.dedupe_key() != a.dedupe_key()
    other = NoticeEvent(
        kind=EventKind.NOTICE,
        channel=_group(),
        actor=_actor("u2"),
        notice_type="group_increase",
        raw={"time": 1700000000},
    )
    assert other.dedupe_key() != a.dedupe_key()


# --------------------------------------------------------------------------- #
# 中间件
# --------------------------------------------------------------------------- #
def _ctx(event: object, adapter: MockAdapter) -> PipelineContext:
    channel = event.channel  # type: ignore[attr-defined]
    return PipelineContext(event=event, reply=Reply(adapter, channel), trace_id="t")  # type: ignore[arg-type]


async def test_middleware_drops_duplicate_request(cfg: object, runtime: object) -> None:
    """同一份加群申请被上报两次：第二次必须在中间件被停掉。"""
    app = App(cfg)  # type: ignore[arg-type]
    app.runtime = runtime  # type: ignore[assignment]
    adapter = MockAdapter()
    event = RequestEvent(
        kind=EventKind.REQUEST,
        channel=_group(),
        actor=_actor(),
        request_type="group",
        sub_type="add",
        flag="FLAG-1",
    )

    await app._mw_dedupe(_ctx(event, adapter))  # 第一次放行

    duplicate = RequestEvent(
        kind=EventKind.REQUEST,
        channel=_group(),
        actor=_actor(),
        request_type="group",
        sub_type="add",
        flag="FLAG-1",
    )
    with pytest.raises(StopPipeline) as exc:
        await app._mw_dedupe(_ctx(duplicate, adapter))
    assert "duplicate" in str(exc.value)


async def test_middleware_lets_two_pokes_through(cfg: object, runtime: object) -> None:
    app = App(cfg)  # type: ignore[arg-type]
    app.runtime = runtime  # type: ignore[assignment]
    adapter = MockAdapter()

    for _ in range(2):
        poke = NoticeEvent(
            kind=EventKind.NOTICE,
            channel=_group(),
            actor=_actor(),
            notice_type="notify",
            sub_type="poke",
            raw={"time": 1700000000},
        )
        await app._mw_dedupe(_ctx(poke, adapter))  # 不该抛
