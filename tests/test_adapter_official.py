"""官方适配器：msg_seq 两枪、富媒体上传、被动窗口降级。

全程用 httpx.MockTransport，不联网、不消耗任何真实额度。
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from haiqbot.adapters.official import MSG_MEDIA, MSG_TEXT, OfficialAdapter, OfficialClient
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.reply import Reply
from haiqbot.core.segment import Segment

API = "https://api.sgroup.qq.com"
TOKEN = "https://bots.qq.com/app/getAppAccessToken"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: dict = {}
        if request.content:
            body = json.loads(request.content)
        self.calls.append((str(request.url), request.headers.get("Authorization", ""), body))

        if str(request.url) == TOKEN:
            return httpx.Response(200, json={"access_token": "tok-xyz", "expires_in": "7200"})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"file_info": "FILE_INFO_1", "ttl": 0, "file_uuid": "u1"})
        return httpx.Response(200, json={"id": "MSG_OUT", "timestamp": 1700000000})

    def payloads(self, suffix: str) -> list[dict]:
        return [body for url, _auth, body in self.calls if url.endswith(suffix)]


def make_adapter(rec: Recorder) -> OfficialAdapter:
    client = OfficialClient(
        app_id="app123",
        secret_provider=lambda: "app-secret-value",
        api_base=API,
        token_url=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(rec.handler)),
    )
    return OfficialAdapter(client=client)


def test_capabilities_reflect_official_constraints() -> None:
    adapter = OfficialAdapter(
        client=OfficialClient(app_id="a", secret_provider=lambda: "s", client=httpx.AsyncClient())
    )
    caps = adapter.capabilities
    assert caps.image is True
    assert caps.image_by_url is False, "官方必须走 /files 上传，不能直发 URL"
    assert caps.window_ttl == 300.0, "群聊被动窗口 5 分钟"


async def test_two_shot_reply_uses_increasing_msg_seq() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    channel = ChannelId("official", ChannelKind.GROUP, "G1")
    reply = Reply(
        adapter,
        channel,
        capabilities=adapter.capabilities,
        source_msg_id="IN_MSG",
        deadline=time.time() + 300,
    )

    await reply.ack("排队中")
    await reply.image("https://example.com/i/abc123")

    messages = rec.payloads("/messages")
    assert len(messages) == 2

    # 第一枪：文本
    assert messages[0]["msg_type"] == MSG_TEXT
    assert messages[0]["msg_id"] == "IN_MSG"
    assert messages[0]["msg_seq"] == 1

    # 第二枪：富媒体
    assert messages[1]["msg_type"] == MSG_MEDIA
    assert messages[1]["msg_seq"] == 2
    assert messages[1]["media"] == {"file_info": "FILE_INFO_1"}
    assert messages[1]["content"] == " ", "官方已知问题：msg_type=7 时 content 必须非空"

    # 上传走的是 /files，且用的是干净 URL
    files = rec.payloads("/files")
    assert len(files) == 1
    assert files[0]["file_type"] == 1
    assert files[0]["srv_send_msg"] is False
    assert files[0]["url"] == "https://example.com/i/abc123"


async def test_token_fetched_once_and_cached() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    channel = ChannelId("official", ChannelKind.GROUP, "G1")
    await adapter.send_message(channel, [Segment.text("a")], reply_to="M", seq=1)
    await adapter.send_message(channel, [Segment.text("b")], reply_to="M", seq=2)

    token_calls = [c for c in rec.calls if c[0] == TOKEN]
    assert len(token_calls) == 1
    assert token_calls[0][2] == {"appId": "app123", "clientSecret": "app-secret-value"}
    # 业务请求带 QQBot 前缀的 Authorization
    auth = [c[1] for c in rec.calls if c[0].endswith("/messages")]
    assert all(a == "QQBot tok-xyz" for a in auth)


async def test_expired_window_sends_nothing() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    channel = ChannelId("official", ChannelKind.GROUP, "G1")
    reply = Reply(
        adapter,
        channel,
        capabilities=adapter.capabilities,
        source_msg_id="IN",
        deadline=time.time() - 1,  # 已经过期
    )

    assert await reply.text("x") is None
    assert await reply.image("https://example.com/i/a") is None
    assert rec.calls == [], "窗口过期后不该发出任何请求"


async def test_window_remaining_and_almost_expired() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    channel = ChannelId("official", ChannelKind.GROUP, "G1")

    fresh = Reply(adapter, channel, source_msg_id="M", deadline=time.time() + 300)
    assert 290 < fresh.remaining() <= 300
    assert fresh.almost_expired(margin=30) is False
    assert fresh.expired() is False

    tight = Reply(adapter, channel, source_msg_id="M", deadline=time.time() + 10)
    assert tight.almost_expired(margin=30) is True


async def test_no_window_means_unlimited() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    # OneBot 式无窗口
    from haiqbot.core.reply import PlatformCapabilities

    reply = Reply(
        adapter,
        ChannelId("official", ChannelKind.GROUP, "G1"),
        capabilities=PlatformCapabilities(image=True, window_ttl=None),
        source_msg_id="M",
        deadline=None,
    )
    assert reply.remaining() is None
    assert reply.expired() is False
    assert reply.almost_expired() is False


async def test_group_and_c2c_endpoints() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    await adapter.send_message(ChannelId("official", ChannelKind.GROUP, "G1"), [Segment.text("g")], seq=1)
    await adapter.send_message(ChannelId("official", ChannelKind.PRIVATE, "U1"), [Segment.text("u")], seq=1)

    urls = [c[0] for c in rec.calls if c[0].endswith("/messages")]
    assert any("/v2/groups/G1/messages" in u for u in urls)
    assert any("/v2/users/U1/messages" in u for u in urls)


async def test_unsupported_channel_raises() -> None:
    rec = Recorder()
    adapter = make_adapter(rec)
    from haiqbot.core.errors import AdapterError

    with pytest.raises(AdapterError):
        await adapter.send_message(
            ChannelId("official", ChannelKind.GUILD_CHANNEL, "C1"), [Segment.text("x")]
        )
