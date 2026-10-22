"""Mock 生图 provider：本地演示与测试用，不联网。"""

from __future__ import annotations

import asyncio

# 1x1 透明 PNG
_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class MockImageProvider:
    name = "mock"

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def generate(self, prompt: str, size: str | None = None) -> tuple[bytes, str]:
        self.calls.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        return _PNG, "image/png"
