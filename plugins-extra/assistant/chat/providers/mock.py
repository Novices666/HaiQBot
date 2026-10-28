"""Mock 对话 provider：本地演示与测试用，不联网。"""

from __future__ import annotations

import asyncio


class MockChatProvider:
    """返回固定文本，用于演示与单测。"""

    name = "mock"

    def __init__(self, *, reply: str = "（模拟回复）", delay: float = 0.0) -> None:
        self.reply = reply
        self.delay = delay
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
        deadline: float | None = None,
    ) -> str:
        self.calls.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return f"{self.reply}{last_user}"
