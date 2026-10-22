"""OneBot 图片「直发」：base64 内嵌，不依赖公网 URL。"""

from __future__ import annotations

import base64
from pathlib import Path

from haiqbot.adapters.mock import MockAdapter
from haiqbot.adapters.onebot11 import OneBotAdapter, _encode_segments
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.reply import ImageRef, Reply
from haiqbot.core.segment import Segment

PNG = b"\x89PNG\r\n\x1a\n" + b"payload" * 4


def _seg_with_file(tmp_path: Path, name: str = "a.png") -> tuple[Segment, Path]:
    path = tmp_path / name
    path.write_bytes(PNG)
    return Segment.image(url="http://127.0.0.1:8080/i/abc", path=str(path)), path


def test_onebot_declares_base64_capability() -> None:
    adapter = OneBotAdapter(OneBotCfg(enabled=True), lambda: None)
    assert adapter.capabilities.image is True
    assert adapter.capabilities.image_by_base64 is True


def test_auto_embeds_base64_when_local_file_exists(tmp_path: Path) -> None:
    seg, path = _seg_with_file(tmp_path)
    out = _encode_segments([seg], None)
    file_field = out[0]["data"]["file"]

    assert file_field.startswith("base64://")
    decoded = base64.b64decode(file_field.removeprefix("base64://"))
    assert decoded == PNG
    assert "127.0.0.1" not in file_field, "直发路径不该把内网 URL 塞进消息"


def test_auto_falls_back_to_url_without_local_file() -> None:
    out = _encode_segments([Segment.image(url="https://cdn.example.com/i/x.png")], None)
    assert out[0]["data"]["file"] == "https://cdn.example.com/i/x.png"


def test_transport_url_forces_url_even_with_file(tmp_path: Path) -> None:
    seg, _ = _seg_with_file(tmp_path)
    out = _encode_segments([seg], None, transport="url")
    assert out[0]["data"]["file"] == "http://127.0.0.1:8080/i/abc"


def test_falls_back_when_file_too_large(tmp_path: Path) -> None:
    seg, _ = _seg_with_file(tmp_path)
    out = _encode_segments([seg], None, max_bytes=4)
    assert out[0]["data"]["file"] == "http://127.0.0.1:8080/i/abc", "超限应改发 URL"


def test_falls_back_when_file_missing(tmp_path: Path) -> None:
    seg = Segment.image(url="http://127.0.0.1:8080/i/abc", path=str(tmp_path / "nope.png"))
    out = _encode_segments([seg], None)
    assert out[0]["data"]["file"] == "http://127.0.0.1:8080/i/abc"


async def test_send_message_embeds_base64_end_to_end(tmp_path: Path) -> None:
    """走完整 send_message：base64 直接进 OneBot 的 message 数组。"""
    cfg = OneBotCfg(enabled=True, image_transport="auto")
    adapter = OneBotAdapter(cfg, lambda: "tok")
    captured: list[tuple[str, dict]] = []

    async def fake_call(action: str, params: dict, *, timeout: float | None = None) -> dict:
        captured.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    adapter.call = fake_call  # type: ignore[method-assign]

    path = tmp_path / "b.png"
    path.write_bytes(PNG)
    await adapter.send_message(
        ChannelId("onebot", ChannelKind.GROUP, "1001"),
        [Segment.text("给你"), Segment.image(url="http://127.0.0.1:8080/i/zzz", path=str(path))],
    )

    action, params = captured[0]
    assert action == "send_group_msg"
    image_seg = [s for s in params["message"] if s["type"] == "image"][0]
    assert image_seg["data"]["file"].startswith("base64://")


async def test_reply_image_carries_local_path(tmp_path: Path) -> None:
    """Reply.image 必须把本地路径透传给适配器，否则没法内嵌。"""
    adapter = MockAdapter()
    reply = Reply(adapter, ChannelId("mock", ChannelKind.GROUP, "1"), capabilities=adapter.capabilities)

    path = tmp_path / "c.png"
    path.write_bytes(PNG)
    await reply.image(ImageRef(media_id="m1", url="http://x/i/m1", path=path))

    sent = adapter.last()
    assert sent is not None
    image_seg = sent["segments"][0]
    assert image_seg["kind"] == "image"
    assert image_seg["data"]["path"] == str(path)
