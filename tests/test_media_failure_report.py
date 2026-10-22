"""媒体发送失败的处理：不重试、把原因说清楚、别把凭据带出去。"""

from __future__ import annotations

import pytest

from haiqbot.builtin_plugins.painter import _safe_reason
from haiqbot.config import HaiqConfig
from haiqbot.core.errors import MediaSendError
from haiqbot.kernel.outbound import OutboundLimiter


@pytest.mark.asyncio
async def test_media_send_error_is_not_retried():
    """媒体被平台拒绝时重试只会加重风控，所以只发一次。"""
    cfg = HaiqConfig().outbound.model_copy(
        update={
            "enabled": True,
            "retry_max": 3,
            "retry_backoff_seconds": 0.01,
            "retry_max_backoff_seconds": 0.01,
        }
    )
    limiter = OutboundLimiter(cfg)
    calls = 0

    async def fn():
        nonlocal calls
        calls += 1
        raise MediaSendError("媒体消息未送达（疑似 QQ 媒体风控）")

    with pytest.raises(MediaSendError):
        await limiter.send("onebot:group:1", fn)
    assert calls == 1
    assert limiter.retried_total == 0


def test_safe_reason_masks_secrets():
    err = RuntimeError("provider 401: key=sk-live-abcdef123456&token=abc Bearer xyz123")
    text = _safe_reason(err)
    assert "sk-live" not in text
    assert "abc123" not in text
    assert text.startswith("RuntimeError:")


def test_safe_reason_handles_empty_message():
    assert _safe_reason(ValueError("")) == "ValueError"
