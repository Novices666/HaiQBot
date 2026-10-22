"""发送超时选择：纯文本走短超时，带媒体走长超时。

背景：2.8 MB base64 图经 NapCat 上传要 15s 以上，用控制类 API 的 15s 超时会误判失败
（图其实已经发出去了）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.segment import Segment

GROUP = ChannelId("onebot", ChannelKind.GROUP, "1001")


def _adapter(**cfg_kwargs: Any) -> OneBotAdapter:
    base: dict[str, Any] = {
        "enabled": True,
        "api_timeout_seconds": 15.0,
        "send_timeout_seconds": 120.0,
    }
    base.update(cfg_kwargs)
    return OneBotAdapter(OneBotCfg(**base), lambda: "tok")


async def _capture_timeout(adapter: OneBotAdapter, segments: list[Segment]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_call(action: str, params: dict, *, timeout: float | None = None) -> dict:
        seen["action"] = action
        seen["timeout"] = timeout
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

    adapter.call = fake_call  # type: ignore[method-assign]
    await adapter.send_message(GROUP, segments)
    return seen


def test_defaults() -> None:
    cfg = OneBotCfg()
    assert cfg.api_timeout_seconds == 15.0
    assert cfg.send_timeout_seconds == 120.0
    assert cfg.send_timeout_seconds > cfg.api_timeout_seconds


async def test_text_only_uses_default_timeout() -> None:
    seen = await _capture_timeout(_adapter(), [Segment.text("hi")])
    assert seen["action"] == "send_group_msg"
    assert seen["timeout"] is None, "纯文本不该传自定义超时，沿用 api_timeout_seconds"


async def test_text_plus_image_uses_send_timeout() -> None:
    seen = await _capture_timeout(_adapter(), [Segment.text("看图"), Segment.image(url="http://x/i/a")])
    assert seen["timeout"] == 120.0


async def test_image_only_uses_send_timeout() -> None:
    seen = await _capture_timeout(_adapter(), [Segment.image(url="http://x/i/a")])
    assert seen["timeout"] == 120.0


async def test_file_segment_uses_send_timeout() -> None:
    seen = await _capture_timeout(_adapter(), [Segment.file(url="http://x/f")])
    assert seen["timeout"] == 120.0


async def test_custom_send_timeout_is_honored(tmp_path: Path) -> None:
    seen = await _capture_timeout(_adapter(send_timeout_seconds=300.0), [Segment.image(url="http://x/i/a")])
    assert seen["timeout"] == 300.0
