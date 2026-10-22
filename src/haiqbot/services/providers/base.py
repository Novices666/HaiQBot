"""provider 抽象。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from haiqbot.core.errors import HaiqError


class ProviderError(HaiqError):
    """provider 调用失败。注意：message 里绝不能带 API key。"""


@runtime_checkable
class ImageProvider(Protocol):
    name: str

    async def generate(self, prompt: str, size: str | None = None) -> tuple[bytes, str]:
        """返回 (图片字节, mime)。"""
        ...
