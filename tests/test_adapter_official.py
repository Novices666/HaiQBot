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
from haiqbot.core.errors import AdapterError, MediaSendError
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


# --------------------------------------------------------------------------- #
# 官方通道的失败语义与健康度（代码层面适配；未真机验证，等 AppID/AppSecret）
# --------------------------------------------------------------------------- #


def make_adapter_with(handler) -> OfficialAdapter:
    client = OfficialClient(
        app_id="app123",
        secret_provider=lambda: "app-secret-value",
        api_base=API,
        token_url=TOKEN,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OfficialAdapter(client=client)


async def test_media_without_url_raises_instead_of_silently_dropping() -> None:
    """只有本地路径的图片：官方 /files 的 url 必填、file_data 未支持 → 明确报错。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == TOKEN:
            return httpx.Response(200, json={"access_token": "t", "expires_in": "7200"})
        return httpx.Response(200, json={"id": "M"})

    adapter = make_adapter_with(handler)
    with pytest.raises(MediaSendError) as exc:
        await adapter.send_message(
            ChannelId("official", ChannelKind.GROUP, "G1"),
            [Segment.image(path="data/media/a.png")],
            reply_to="IN",
            seq=1,
        )
    assert "公网 URL" in str(exc.value)
    assert not [u for u in calls if u.endswith("/files")], "没有 URL 就不该去上传"


async def test_media_fetch_failure_maps_to_media_error() -> None:
    """304082/304083 = 平台拉不到我们给的 URL：不重试，报「发送失败」。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN:
            return httpx.Response(200, json={"access_token": "t", "expires_in": "7200"})
        if request.url.path.endswith("/files"):
            return httpx.Response(400, json={"code": 304082, "message": "upload media info fail"})
        return httpx.Response(200, json={"id": "M"})

    adapter = make_adapter_with(handler)
    with pytest.raises(MediaSendError) as exc:
        await adapter.send_message(
            ChannelId("official", ChannelKind.GROUP, "G1"),
            [Segment.image(url="http://127.0.0.1:8090/i/abc")],
            reply_to="IN",
            seq=1,
        )
    assert "304082" in str(exc.value)
    assert "公网可达" in str(exc.value)


async def test_rate_limit_code_is_retryable() -> None:
    """22009 是限速而非媒体问题：应被出站闸门判成可重试。"""
    from haiqbot.kernel.outbound import is_rate_limited

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN:
            return httpx.Response(200, json={"access_token": "t", "expires_in": "7200"})
        return httpx.Response(400, json={"code": 22009, "message": "msg limit exceed"})

    adapter = make_adapter_with(handler)
    with pytest.raises(AdapterError) as exc:
        await adapter.send_message(
            ChannelId("official", ChannelKind.GROUP, "G1"), [Segment.text("hi")], reply_to="IN", seq=1
        )
    assert not isinstance(exc.value, MediaSendError)
    assert is_rate_limited(exc.value) is True


async def test_media_upload_without_file_info_is_media_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN:
            return httpx.Response(200, json={"access_token": "t", "expires_in": "7200"})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"file_uuid": "u1", "ttl": 0})
        return httpx.Response(200, json={"id": "M"})

    adapter = make_adapter_with(handler)
    with pytest.raises(MediaSendError):
        await adapter.send_message(
            ChannelId("official", ChannelKind.GROUP, "G1"),
            [Segment.image(url="https://example.com/a.png")],
            reply_to="IN",
            seq=1,
        )


def test_health_reports_readiness() -> None:
    adapter = OfficialAdapter(client=OfficialClient(app_id="a", secret_provider=lambda: "s"))
    assert adapter.health()["connected"] is None, "没 start 过不该声称已连接"
    adapter.mark_started()
    assert adapter.health()["connected"] is False
    adapter._ready = True
    health = adapter.health()
    assert health["connected"] is True
    assert health["mode"] == "ws"


async def test_declares_only_implemented_actions() -> None:
    adapter = OfficialAdapter(client=OfficialClient(app_id="a", secret_provider=lambda: "s"))
    assert "can_send_image" in adapter.actions
    assert await adapter.can_send_image() is True
    assert await adapter.can_send_record() is True
    # 官方没有群管理接口，绝不能声称支持（插件会据此写降级文案）
    for name in ("mute", "recall", "kick", "member_list"):
        assert name not in adapter.actions


async def test_group_at_content_leading_space_stripped() -> None:
    """`@机器人 /echo hi` 的 content 是 `" /echo hi"`：不剥前导空白就命中不了命令。"""
    from types import SimpleNamespace

    events = []

    async def push(event) -> None:
        events.append(event)

    adapter = OfficialAdapter(client=OfficialClient(app_id="a", secret_provider=lambda: "s"), push=push)
    message = SimpleNamespace(
        id="MID",
        group_openid="G1",
        content=" /echo hi",
        timestamp="2026-01-01T00:00:00+08:00",
        author=SimpleNamespace(member_openid="U1"),
    )
    await adapter._on_group(message)

    assert len(events) == 1
    event = events[0]
    assert event.segments[0].data["text"] == "/echo hi"
    assert event.msg_id == "MID"
    assert event.at_bot is True
    assert event.raw["event"] == "GROUP_AT_MESSAGE_CREATE"


async def test_botpy_client_wiring_matches_sdk() -> None:
    """和真 SDK 对一遍：intents 位、handler 名。

    botpy 的 dispatch 规则是 `method = "on_" + 事件名`（botpy/client.py），
    方法名差一个字事件就静默丢失 —— 这种错在真机上表现为「bot 上线了但不回话」，
    离线能测就测掉。没装 qq-botpy 时整体 skip。
    """
    botpy = pytest.importorskip("botpy")

    from haiqbot.adapters.official import INTENT_PUBLIC_MESSAGES

    adapter = OfficialAdapter(
        client=OfficialClient(app_id="a", secret_provider=lambda: "s"),
        intents=INTENT_PUBLIC_MESSAGES,
    )
    client = adapter._make_botpy_client(botpy)

    # botpy 的 Client 把 intents 存成整数（intents.value），不是对象
    assert int(client.intents) & INTENT_PUBLIC_MESSAGES, "群@ / 单聊的 intent 位没开"
    for name in ("on_ready", "on_group_at_message_create", "on_c2c_message_create"):
        assert callable(getattr(client, name, None)), f"缺 handler：{name}"
