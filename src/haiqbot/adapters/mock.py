"""Mock 适配器：本地演示与测试用。不连 QQ、不烧额度。"""

from __future__ import annotations

from typing import Any

from haiqbot.adapters.base import Adapter, PushFn
from haiqbot.core.channel import ChannelId
from haiqbot.core.event import Event
from haiqbot.core.reply import PlatformCapabilities
from haiqbot.core.segment import Segment


class MockAdapter(Adapter):
    platform = "mock"
    bot_id = "mock-bot"
    capabilities = PlatformCapabilities(
        image=True,
        markdown=True,
        button=True,
        file=True,
        image_by_url=True,
        window_ttl=None,
    )

    def __init__(self, push: PushFn | None = None) -> None:
        super().__init__(push)
        self.sent: list[dict[str, Any]] = []
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def send_message(
        self,
        channel: ChannelId,
        segments: list[Segment],
        *,
        reply_to: str | None = None,
        seq: int | None = None,
        plugin_id: str | None = None,
    ) -> str | None:
        message_id = f"mock-{len(self.sent) + 1}"
        self.sent.append(
            {
                "id": message_id,
                "channel": str(channel),
                "segments": [s.to_dict() for s in segments],
                "reply_to": reply_to,
                "seq": seq,
                "plugin_id": plugin_id,
                "text": "".join(s.data.get("text", "") for s in segments if s.kind == "text"),
            }
        )
        return message_id

    # -- 测试辅助 --------------------------------------------------------- #
    async def inject(self, event: Event) -> None:
        await self._emit(event)

    def last(self) -> dict[str, Any] | None:
        return self.sent[-1] if self.sent else None

    def texts(self) -> list[str]:
        return [m["text"] for m in self.sent]

    def reset(self) -> None:
        self.sent.clear()
