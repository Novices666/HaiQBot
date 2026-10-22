"""消息段编解码与纯文本抽取。"""

from __future__ import annotations

from haiqbot.core.segment import Segment, SegmentKind, has_at_bot, segments_to_text


def test_text_segment_roundtrip() -> None:
    seg = Segment.text("你好")
    assert seg.kind is SegmentKind.TEXT
    assert Segment.from_dict(seg.to_dict()) == seg


def test_all_constructors() -> None:
    assert Segment.image(url="http://x/i/a").data == {"url": "http://x/i/a"}
    assert Segment.image(media_id="abc").data == {"media_id": "abc"}
    assert Segment.at("all").kind is SegmentKind.AT_ALL
    assert Segment.at("123").kind is SegmentKind.AT
    assert Segment.reply("m1").data == {"msg_id": "m1"}
    assert Segment.file(url="http://x/f", name="a.txt").data["name"] == "a.txt"


def test_segments_to_text_ignores_non_text() -> None:
    segs = [Segment.at("1"), Segment.text("hello"), Segment.image(url="u"), Segment.text(" world")]
    assert segments_to_text(segs) == "hello world"


def test_has_at_bot() -> None:
    segs = [Segment.at("42")]
    assert has_at_bot(segs, "42") is True
    assert has_at_bot(segs, "43") is False
    assert has_at_bot(segs, None) is False


def test_unknown_kind_preserved() -> None:
    seg = Segment.from_dict({"kind": "face", "data": {"id": 1}})
    assert seg.kind is SegmentKind.UNKNOWN or seg.kind is SegmentKind.FACE
