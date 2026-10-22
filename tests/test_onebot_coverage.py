"""OneBot 覆盖修复：失败校验 / 未知段内容 / raw 完整。"""

from __future__ import annotations

import pytest

from haiqbot.adapters.onebot11 import OneBotAdapter, _check_response, _decode_segments
from haiqbot.config import OneBotCfg
from haiqbot.core.errors import AdapterError
from haiqbot.core.segment import SegmentKind, segments_to_text


# --------------------------------------------------------------------------- #
# 1) 失败不再"假成功"
# --------------------------------------------------------------------------- #
def test_check_response_raises_on_failed() -> None:
    with pytest.raises(AdapterError) as exc:
        _check_response("delete_msg", {"status": "failed", "retcode": 100, "message": "权限不足"})
    text = str(exc.value)
    assert "delete_msg" in text
    assert "100" in text
    assert "权限不足" in text


def test_check_response_accepts_ok() -> None:
    resp = {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
    assert _check_response("send_msg", resp) is resp


def test_check_response_accepts_async_retcode_one() -> None:
    assert _check_response("x", {"status": "async", "retcode": 1})["status"] == "async"


def test_check_response_raises_when_retcode_bad_even_without_status() -> None:
    with pytest.raises(AdapterError):
        _check_response("x", {"retcode": 1200, "wording": "风控"})


def test_check_response_passes_through_non_dict() -> None:
    assert _check_response("x", None) == {"status": "ok", "data": None}


# --------------------------------------------------------------------------- #
# 2) 未知段不再丢内容
# --------------------------------------------------------------------------- #
def test_unknown_segment_keeps_scalar_fields() -> None:
    segs = _decode_segments([{"type": "dice", "data": {"result": "3"}}])
    assert segs[0].kind is SegmentKind.UNKNOWN
    assert segs[0].data["raw_type"] == "dice"
    assert segs[0].data["result"] == "3"


def test_unknown_segment_gets_placeholder_text() -> None:
    for seg_type, label in (
        ("dice", "[骰子]"),
        ("rps", "[猜拳]"),
        ("poke", "[戳一戳]"),
        ("location", "[位置]"),
        ("json", "[卡片]"),
        ("mface", "[表情]"),
    ):
        segs = _decode_segments([{"type": seg_type, "data": {}}])
        assert segments_to_text(segs) == label, seg_type


def test_unknown_segment_prefers_visible_text() -> None:
    """卡片正文能抽出来就不该只显示"[卡片]"。"""
    segs = _decode_segments([{"type": "json", "data": {"prompt": "[QQ小程序] 某某签到"}}])
    assert segments_to_text(segs) == "[QQ小程序] 某某签到"


def test_known_segments_unaffected() -> None:
    segs = _decode_segments(
        [
            {"type": "text", "data": {"text": "你好"}},
            {"type": "at", "data": {"qq": "42"}},
        ]
    )
    assert segments_to_text(segs) == "你好"


# --------------------------------------------------------------------------- #
# 3) message.raw 完整
# --------------------------------------------------------------------------- #
def _adapter() -> OneBotAdapter:
    return OneBotAdapter(OneBotCfg(), token_provider=lambda: None)


def test_parse_message_keeps_full_raw() -> None:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 100,
        "user_id": 7,
        "message_id": 9,
        "self_id": 42,
        "raw_message": "hi",
        "sender": {"nickname": "n", "role": "admin"},
        "anonymous": None,
        "message": [{"type": "text", "data": {"text": "hi"}}],
    }
    event = _adapter()._parse_message(payload)
    assert event is not None
    assert event.raw["raw_message"] == "hi"
    assert event.raw["sender"]["role"] == "admin"
    assert "anonymous" in event.raw
    assert event.text == "hi"


def test_parse_message_mentions_bot_uses_segments() -> None:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 100,
        "user_id": 7,
        "message_id": 9,
        "self_id": 42,
        "message": [{"type": "at", "data": {"qq": "42"}}],
    }
    event = _adapter()._parse_message(payload)
    assert event is not None
    assert event.mentions_bot() is True
