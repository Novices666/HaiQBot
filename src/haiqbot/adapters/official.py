"""QQ 官方开放平台适配器。

拆成两层，方便离线测试：
- `OfficialClient`：纯 httpx 的 API 客户端（token 管理 + 群/C2C 发消息 + 富媒体上传）
- `OfficialAdapter`：用 botpy 收 WS 事件，用 `OfficialClient` 发消息

平台硬约束（已核实官方文档）：
- 主动推送自 2025-04-21 起下线 → 只能被动回复
- 群聊被动窗口 300 秒，同一 msg_id 最多回 5 次 → `msg_seq` 必须递增
- 富媒体必须先 `/files` 上传拿 `file_info`，再 `msg_type=7` 发出去
- `msg_type=7` 时 `content` 必须填一个空格（官方已知问题）
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from haiqbot.adapters.base import Adapter, PushFn
from haiqbot.core.channel import Actor, ChannelId, ChannelKind, Role
from haiqbot.core.errors import AdapterError
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.reply import PlatformCapabilities
from haiqbot.core.segment import Segment, SegmentKind
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)

MSG_TEXT = 0
MSG_MARKDOWN = 2
MSG_MEDIA = 7
# /files 的 file_type：1 图片 / 2 视频 / 3 语音 / 4 文件（官方暂未开放）
FILE_TYPE_IMAGE = 1
FILE_TYPE_VIDEO = 2
FILE_TYPE_AUDIO = 3

_FILE_TYPE = {
    SegmentKind.IMAGE: FILE_TYPE_IMAGE,
    SegmentKind.VIDEO: FILE_TYPE_VIDEO,
    SegmentKind.RECORD: FILE_TYPE_AUDIO,
}


class OfficialClient:
    """官方 OpenAPI 的最小客户端。所有请求都带 `Authorization: QQBot <token>`。"""

    def __init__(
        self,
        *,
        app_id: str,
        secret_provider: Callable[[], str],
        api_base: str = "https://api.sgroup.qq.com",
        token_url: str = "https://bots.qq.com/app/getAppAccessToken",
        client: Any = None,
    ) -> None:
        self.app_id = app_id
        self._secret_provider = secret_provider
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self._client = client
        self._token: str | None = None
        self._expire_at: float = 0.0
        self._lock = asyncio.Lock()

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
            self._client = None

    # -- token ------------------------------------------------------------ #
    async def token(self, *, force: bool = False) -> str:
        async with self._lock:
            if not force and self._token and time.time() < self._expire_at:
                return self._token
            secret = self._secret_provider()  # 由 vault 提供，已登记脱敏
            resp = await self._http().post(
                self.token_url,
                json={"appId": self.app_id, "clientSecret": secret},
            )
            if resp.status_code >= 400:
                raise AdapterError(f"获取 access_token 失败（HTTP {resp.status_code}）")
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise AdapterError("access_token 响应缺少 access_token 字段")
            expires = float(body.get("expires_in") or 7200)
            self._token = token
            # 提前 60 秒刷新
            self._expire_at = time.time() + max(30.0, expires - 60.0)
            log.info("official_token_refreshed", expires_in=expires)
            return token

    async def _headers(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {await self.token()}", "Content-Type": "application/json"}

    # -- 发送 ------------------------------------------------------------- #
    async def post_group_message(self, group_openid: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/v2/groups/{group_openid}/messages"
        return await self._post(url, payload)

    async def post_c2c_message(self, user_openid: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.api_base}/v2/users/{user_openid}/messages"
        return await self._post(url, payload)

    async def post_group_file(
        self, group_openid: str, media_url: str, *, file_type: int = 1, srv_send_msg: bool = False
    ) -> dict[str, Any]:
        url = f"{self.api_base}/v2/groups/{group_openid}/files"
        return await self._post(url, {"file_type": file_type, "url": media_url, "srv_send_msg": srv_send_msg})

    async def post_c2c_file(
        self, user_openid: str, media_url: str, *, file_type: int = 1, srv_send_msg: bool = False
    ) -> dict[str, Any]:
        url = f"{self.api_base}/v2/users/{user_openid}/files"
        return await self._post(url, {"file_type": file_type, "url": media_url, "srv_send_msg": srv_send_msg})

    async def _post(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http().post(url, json=payload, headers=await self._headers())
        if resp.status_code >= 400:
            code = ""
            try:
                code = str(resp.json().get("code", ""))
            except Exception:  # noqa: BLE001
                pass
            raise AdapterError(f"官方接口 {resp.status_code}{' code=' + code if code else ''}")
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}


class OfficialAdapter(Adapter):
    platform = "official"
    capabilities = PlatformCapabilities(
        image=True,
        markdown=True,
        button=True,
        file=False,
        image_by_url=False,  # 必须走 /files 上传
        # 语音/视频：官方没有消息段，但 /files 的 file_type 支持 3=语音 2=视频。
        # 注意资源格式：语音要 silk，视频要 mp4，否则平台侧拒收。
        record=True,
        video=True,
        window_ttl=300.0,
    )

    def __init__(
        self,
        *,
        client: OfficialClient,
        intents: int = 1 << 25,
        push: PushFn | None = None,
    ) -> None:
        super().__init__(push)
        self.client = client
        self.intents = intents
        self._botpy_client: Any = None
        self._task: asyncio.Task[None] | None = None

    # -- 生命周期 --------------------------------------------------------- #
    async def start(self) -> None:
        try:
            import botpy  # noqa: F401
        except ImportError as exc:
            raise AdapterError("官方平台适配需要 qq-botpy：`uv sync --extra official` 后再启动") from exc
        self._task = asyncio.create_task(self._run_botpy(), name="haiqbot:official-ws")
        log.info("official_adapter_started", intents=self.intents)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        await self.client.aclose()

    async def _run_botpy(self) -> None:
        import botpy

        adapter = self

        class _Client(botpy.Client):
            async def on_ready(self) -> None:  # noqa: D401
                adapter.bot_id = getattr(getattr(self, "robot", None), "id", None)
                log.info("official_ready", bot=getattr(self, "robot", None) is not None)

            async def on_group_at_message_create(self, message: Any) -> None:
                await adapter._on_group(message)

            async def on_c2c_message_create(self, message: Any) -> None:
                await adapter._on_c2c(message)

        self._botpy_client = _Client(intents=botpy.Intents(public_messages=True))
        try:
            await self._botpy_client.start(appid=self.client.app_id, secret=self.client._secret_provider())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("official_ws_error")

    # -- 事件归一化 ------------------------------------------------------- #
    async def _on_group(self, message: Any) -> None:
        await self._emit(
            MessageEvent(
                kind=EventKind.MESSAGE,
                channel=ChannelId("official", ChannelKind.GROUP, str(message.group_openid)),
                actor=Actor(id=str(getattr(message.author, "member_openid", "")), role=Role.USER),
                segments=[Segment.text(str(getattr(message, "content", "") or ""))],
                msg_id=str(getattr(message, "id", "")),
                at_bot=True,
                bot_id=self.bot_id,
                window_ttl=300.0,
                raw={"event": "GROUP_AT_MESSAGE_CREATE"},
            )
        )

    async def _on_c2c(self, message: Any) -> None:
        await self._emit(
            MessageEvent(
                kind=EventKind.MESSAGE,
                channel=ChannelId(
                    "official", ChannelKind.PRIVATE, str(getattr(message.author, "user_openid", ""))
                ),
                actor=Actor(id=str(getattr(message.author, "user_openid", ""))),
                segments=[Segment.text(str(getattr(message, "content", "") or ""))],
                msg_id=str(getattr(message, "id", "")),
                at_bot=True,
                bot_id=self.bot_id,
                window_ttl=3600.0,
                raw={"event": "C2C_MESSAGE_CREATE"},
            )
        )

    # -- 发送 ------------------------------------------------------------- #
    async def send_message(
        self,
        channel: ChannelId,
        segments: list[Segment],
        *,
        reply_to: str | None = None,
        seq: int | None = None,
        plugin_id: str | None = None,
    ) -> str | None:
        # 一条回复 = 一个出站槽位（文本 + 图片算同一条回复）
        return await self._guarded_send(
            channel,
            lambda: self._raw_send(channel, segments, reply_to=reply_to, seq=seq, plugin_id=plugin_id),
            plugin_id=plugin_id,
        )

    async def _raw_send(
        self,
        channel: ChannelId,
        segments: list[Segment],
        *,
        reply_to: str | None = None,
        seq: int | None = None,
        plugin_id: str | None = None,
    ) -> str | None:
        text = "".join(s.data.get("text", "") for s in segments if s.kind is SegmentKind.TEXT)
        # 图片 / 语音 / 视频都走"先上传拿 file_info，再发 msg_type=7"
        media_segments = [
            s for s in segments if s.kind in (SegmentKind.IMAGE, SegmentKind.RECORD, SegmentKind.VIDEO)
        ]
        markdown = "\n".join(str(s.data.get("text", "")) for s in segments if s.kind is SegmentKind.MARKDOWN)
        rows = next(
            (s.data.get("rows") for s in segments if s.kind is SegmentKind.KEYBOARD),
            None,
        )
        for seg in segments:
            if seg.kind is SegmentKind.FORWARD:
                # 官方没有合并转发能力，明确告警（Reply.forward 会先降级成文本）
                log.warning("official_segment_unsupported", kind=str(seg.kind), plugin=plugin_id)
        message_id: str | None = None
        if markdown.strip():
            # msg_type=2：markdown 正文 + 可选按钮。content 官方要求非空。
            body: dict[str, Any] = {
                "content": text.strip() or " ",
                "msg_type": MSG_MARKDOWN,
                "markdown": {"content": markdown},
            }
            if rows:
                # keyboard 的内层结构（render_data/action/permission）由调用方决定，
                # 内核只负责套上官方要求的 content.rows 外壳。
                body["keyboard"] = {"content": {"rows": rows}}
            message_id = await self._send_payload(channel, body, reply_to=reply_to, seq=seq)
            seq = (seq or 0) + 1 if seq is not None else None
        elif text.strip():
            message_id = await self._send_payload(
                channel, {"content": text, "msg_type": MSG_TEXT}, reply_to=reply_to, seq=seq
            )
            # 文本已占用一个 seq，图片要用下一个
            seq = (seq or 0) + 1 if seq is not None else None
        for media in media_segments:
            url = media.data.get("url")
            if not url:
                log.warning("official_media_without_url", kind=str(media.kind), plugin=plugin_id)
                continue
            message_id = await self._send_media(
                channel,
                url,
                file_type=_FILE_TYPE.get(media.kind, FILE_TYPE_IMAGE),
                reply_to=reply_to,
                seq=seq,
            )
            if seq is not None:
                seq += 1
        return message_id

    async def _send_payload(
        self,
        channel: ChannelId,
        payload: dict[str, Any],
        *,
        reply_to: str | None,
        seq: int | None,
    ) -> str | None:
        body = dict(payload)
        if reply_to:
            body["msg_id"] = reply_to
            if seq is not None:
                body["msg_seq"] = seq
        if channel.kind is ChannelKind.GROUP:
            data = await self.client.post_group_message(channel.id, body)
        elif channel.kind is ChannelKind.PRIVATE:
            data = await self.client.post_c2c_message(channel.id, body)
        else:
            raise AdapterError(f"官方适配器不支持该频道类型：{channel.kind}")
        return data.get("id")

    async def _send_media(
        self,
        channel: ChannelId,
        media_url: str,
        *,
        file_type: int = FILE_TYPE_IMAGE,
        reply_to: str | None,
        seq: int | None,
    ) -> str | None:
        if channel.kind is ChannelKind.GROUP:
            upload = await self.client.post_group_file(channel.id, media_url, file_type=file_type)
        elif channel.kind is ChannelKind.PRIVATE:
            upload = await self.client.post_c2c_file(channel.id, media_url, file_type=file_type)
        else:
            raise AdapterError(f"官方适配器不支持该频道类型：{channel.kind}")
        file_info = upload.get("file_info")
        if not file_info:
            raise AdapterError("富媒体上传未返回 file_info")
        body: dict[str, Any] = {
            "content": " ",  # 官方已知问题：msg_type=7 时 content 必须非空
            "msg_type": MSG_MEDIA,
            "media": {"file_info": file_info},
        }
        return await self._send_payload(channel, body, reply_to=reply_to, seq=seq)
