"""OneBot v11 适配器：段编解码、事件归一化、发送动作。"""

from __future__ import annotations

from typing import Any

import pytest

from haiqbot.adapters.onebot11 import OneBotAdapter, _decode_segments, _encode_segments
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import ChannelKind
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.segment import Segment


def _adapter(push: Any = None) -> OneBotAdapter:
    return OneBotAdapter(OneBotCfg(enabled=True, url="ws://127.0.0.1:3001"), lambda: "tok", push)


def test_encode_text_and_reply() -> None:
    out = _encode_segments([Segment.text("hi")], "m999")
    assert out[0] == {"type": "reply", "data": {"id": "m999"}}
    assert out[1] == {"type": "text", "data": {"text": "hi"}}


def test_encode_image_uses_url() -> None:
    out = _encode_segments([Segment.image(url="https://example.com/i/abc")], None)
    assert out == [{"type": "image", "data": {"file": "https://example.com/i/abc"}}]


def test_encode_at_and_at_all() -> None:
    assert _encode_segments([Segment.at("123")], None)[0] == {"type": "at", "data": {"qq": "123"}}
    assert _encode_segments([Segment.at("all")], None)[0] == {"type": "at", "data": {"qq": "all"}}


def test_decode_array_form() -> None:
    segs = _decode_segments(
        [
            {"type": "text", "data": {"text": "hello "}},
            {"type": "at", "data": {"qq": "42"}},
            {"type": "image", "data": {"url": "https://x/y.png"}},
            {"type": "reply", "data": {"id": "m1"}},
            {"type": "face", "data": {"id": "1"}},
        ]
    )
    assert segs[0].data["text"] == "hello "
    assert segs[1].kind.value == "at"
    assert segs[2].data["url"] == "https://x/y.png"
    assert segs[3].kind.value == "reply"
    # face 现在是一等消息段（B2 落地），不再退化成 unknown


def test_decode_string_form() -> None:
    segs = _decode_segments("纯文本消息")
    assert len(segs) == 1
    assert segs[0].data["text"] == "纯文本消息"


async def test_event_normalization_group() -> None:
    captured: list[MessageEvent] = []

    async def push(event: Any) -> None:
        captured.append(event)

    adapter = _adapter(push)
    await adapter._handle_event(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1001,
            "user_id": 2002,
            "message_id": 3003,
            "self_id": 4004,
            "sender": {"nickname": "小明"},
            "message": [{"type": "text", "data": {"text": "你好"}}],
        }
    )

    assert len(captured) == 1
    event = captured[0]
    assert event.kind is EventKind.MESSAGE
    assert event.channel.kind is ChannelKind.GROUP
    assert event.channel.platform == "onebot"
    assert event.channel.id == "1001"
    assert event.actor.id == "2002"
    assert event.actor.name == "小明"
    assert event.text == "你好"
    assert event.window_ttl is None, "OneBot 没有被动窗口限制"


async def test_event_normalization_private() -> None:
    captured: list[Any] = []

    async def push(event: Any) -> None:
        captured.append(event)

    adapter = _adapter(push)
    await adapter._handle_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 555,
            "message_id": 1,
            "message": "hi",
        }
    )
    assert captured[0].channel.kind is ChannelKind.PRIVATE
    assert captured[0].channel.id == "555"


async def test_non_message_events_ignored() -> None:
    captured: list[Any] = []

    async def push(event: Any) -> None:
        captured.append(event)

    adapter = _adapter(push)
    await adapter._handle_event({"post_type": "notice", "notice_type": "group_recall"})
    await adapter._handle_event({"post_type": "meta_event", "meta_event_type": "heartbeat"})
    assert captured == []


async def test_send_message_dispatches_correct_action() -> None:
    calls: list[tuple[str, dict]] = []

    async def fake_call(action: str, params: dict) -> dict:
        calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 777}}

    adapter = _adapter()
    adapter.call = fake_call  # type: ignore[method-assign]

    from haiqbot.core.channel import ChannelId

    mid = await adapter.send_message(
        ChannelId("onebot", ChannelKind.GROUP, "1001"), [Segment.text("hi")], reply_to="9"
    )
    assert mid == "777"
    assert calls[0][0] == "send_group_msg"
    assert calls[0][1]["group_id"] == 1001
    assert calls[0][1]["message"][0]["type"] == "reply"

    calls.clear()
    await adapter.send_message(ChannelId("onebot", ChannelKind.PRIVATE, "555"), [Segment.text("x")])
    assert calls[0][0] == "send_private_msg"
    assert calls[0][1]["user_id"] == 555


async def test_send_failure_raises() -> None:
    async def fake_call(action: str, params: dict) -> dict:
        return {"status": "failed", "retcode": 100}

    adapter = _adapter()
    adapter.call = fake_call  # type: ignore[method-assign]

    from haiqbot.core.channel import ChannelId
    from haiqbot.core.errors import AdapterError

    try:
        await adapter.send_message(ChannelId("onebot", ChannelKind.GROUP, "1"), [Segment.text("x")])
    except AdapterError:
        return
    raise AssertionError("应当抛出 AdapterError")


# -- 回执超时（NapCat 假失败）--------------------------------------------------- #

_ACK_TIMEOUT = (
    "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
    "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:"
)


def test_send_ack_timeout_raises_media_send_error():
    """实测：这个超时是**真失败**（QQ 媒体风控），必须报错而不是假装成功。

    早期版本按"已投递"处理，结果群里收不到图也没有任何提示；
    现在改成 MediaSendError，由插件把「生成成功、发送失败」说清楚。
    """
    from haiqbot.adapters.onebot11 import _check_response
    from haiqbot.core.errors import MediaSendError

    with pytest.raises(MediaSendError):
        _check_response("send_group_msg", {"status": "failed", "retcode": 200, "message": _ACK_TIMEOUT})


def test_ack_timeout_on_non_send_action_still_raises():
    from haiqbot.adapters.onebot11 import _check_response
    from haiqbot.core.errors import AdapterError

    with pytest.raises(AdapterError):
        _check_response("get_group_list", {"status": "failed", "retcode": 200, "message": _ACK_TIMEOUT})


def test_other_send_failures_still_raise():
    from haiqbot.adapters.onebot11 import _check_response
    from haiqbot.core.errors import AdapterError

    with pytest.raises(AdapterError):
        _check_response("send_group_msg", {"status": "failed", "retcode": 1200, "message": "格式错误"})


def test_rich_media_failure_is_media_send_error():
    """`rich media transfer failed` 也是媒体失败：必须报 MediaSendError（不重试）。"""
    from haiqbot.adapters.onebot11 import _check_response
    from haiqbot.core.errors import MediaSendError

    resp = {
        "status": "failed",
        "retcode": 200,
        "message": 'EventChecker Failed: NTEvent ... EventRet:\n{\n  "result": -1,\n  "errMsg": "rich media transfer failed"\n}',
    }
    with pytest.raises(MediaSendError):
        _check_response("send_group_msg", resp)


def test_rich_media_failure_on_control_action_still_raises_plain():
    """非 send_* 动作遇到同样字样仍按普通失败处理。"""
    from haiqbot.adapters.onebot11 import _check_response
    from haiqbot.core.errors import MediaSendError

    resp = {"status": "failed", "retcode": 200, "message": "rich media transfer failed"}
    with pytest.raises(Exception) as exc:
        _check_response("get_group_list", resp)
    assert not isinstance(exc.value, MediaSendError)
