"""OneBot v11 适配器。

四种接法都支持（OneBot v11 的全部连接方式）：
- `mode = "client"`：haiqbot 主动连 NapCat 的正向 WebSocket（推荐，最省事）
- `mode = "server"`：haiqbot 起 WS 服务，等 NapCat/SnowLuma 反向连入（仅监听 127.0.0.1 + 强制 token）
- `mode = "http"`：API 走 HTTP POST，事件由 haiqbot 起 HTTP 服务接收（反向 HTTP 上报）
- `mode = "sse"`：API 走 HTTP POST，事件用 SSE 订阅 —— 不需要任何入站端口

OneBot 没有 5 分钟被动窗口，所以 `window_ttl = None`。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from haiqbot.adapters.base import Adapter, PushFn
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import Actor, ChannelId, ChannelKind, Role
from haiqbot.core.errors import AdapterError, MediaSendError
from haiqbot.core.event import Event, EventKind, MessageEvent, NoticeEvent, RequestEvent
from haiqbot.core.reply import PlatformCapabilities
from haiqbot.core.segment import Segment, SegmentKind
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
    from websockets.asyncio.server import serve as ws_serve
except Exception:  # pragma: no cover - 老版本回退
    from websockets import connect as ws_connect  # type: ignore[no-redef]
    from websockets import serve as ws_serve  # type: ignore[no-redef]


#: OneBot 的 `sender.role` 是字符串（owner/admin/member），必须显式映射成 Role。
#: 之前这里直接把 Actor.role 留在默认值 USER —— 结果"群主发的广告也被风控拦"。
_SENDER_ROLE = {"owner": Role.OWNER, "admin": Role.ADMIN, "member": Role.USER}


class OneBotAdapter(Adapter):
    platform = "onebot"
    capabilities = PlatformCapabilities(
        image=True,
        markdown=False,
        button=False,
        file=True,
        image_by_url=True,
        image_by_base64=True,
        record=True,
        video=True,
        forward=True,
        window_ttl=None,
    )

    # OneBot 侧能做的管理动作（插件用 ctx.mod.supports() 探测）
    actions = frozenset(
        {
            "recall",
            "mute",
            "kick",
            "member_list",
            "request",
            "get_message",
            "get_forward",
            "group_list",
            "group_info",
            "member_info",
            "stranger_info",
            "set_group_card",
            "upload_group_file",
            "emoji_like",
            "login_info",
            "status",
            "friend_list",
            "essence_list",
            "set_group_name",
            "leave_group",
            "set_group_whole_ban",
            "send_like",
            "version_info",
            "at_all_remain",
            "honor_list",
            "fetch_image",
            "fetch_record",
            "mark_read",
            "set_essence",
            "del_essence",
            "set_admin",
            "set_title",
            "set_portrait",
            "set_anonymous",
            "ban_anonymous",
            "user_status",
            "unidirectional_friends",
            "sign_info",
            "sign_in",
            "group_files",
            "folder_files",
            "file_url",
            "file_system_info",
            "create_folder",
            "delete_file",
            "delete_folder",
            "can_send_image",
            "can_send_record",
            "system_messages",
        }
    )

    def __init__(
        self,
        cfg: OneBotCfg,
        token_provider: Callable[[], str | None],
        push: PushFn | None = None,
        secret_provider: Callable[[str], str | None] | None = None,
    ) -> None:
        super().__init__(push)
        self.cfg = cfg
        self._token_provider = token_provider
        # 只有 HTTP 上报的 HMAC 校验需要它；WS 模式用不到
        self._secret_provider = secret_provider
        self._http: Any = None
        self._uvicorn: Any = None
        self._ws: Any = None
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._echo = 0
        self._connected = asyncio.Event()
        # 事件必须异步派发，否则「处理事件 → 调 API → 等回包」会和收包循环互相阻塞
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._event_slots = asyncio.Semaphore(16)
        # -- 健康度观测 --
        self._reconnects = 0
        self._connected_since: float | None = None
        self._last_error: str | None = None

    # -- 健康度 --------------------------------------------------------- #
    def health(self) -> dict[str, Any]:
        base = super().health()
        if self.cfg.mode == "client":
            target = self.cfg.url
        elif self.cfg.mode == "server":
            target = f"ws://{self.cfg.host}:{self.cfg.port}"
        else:
            target = self.cfg.http_api_url
        base.update(
            {
                "connected": self._connected.is_set(),
                "mode": self.cfg.mode,
                "target": target,
                "webhook": (
                    f"http://{self.cfg.host}:{self.cfg.port}{self.cfg.webhook_path}"
                    if self.cfg.mode == "http"
                    else None
                ),
                "connected_since": self._connected_since,
                "reconnects": self._reconnects,
                "last_error": self._last_error,
            }
        )
        return base

    # -- 生命周期 --------------------------------------------------------- #
    async def start(self) -> None:
        if self.cfg.mode == "server":
            runner, name = self._run_server, "haiqbot:onebot-server"
        elif self.cfg.mode == "http":
            runner, name = self._run_webhook, "haiqbot:onebot-webhook"
        elif self.cfg.mode == "sse":
            runner, name = self._run_sse, "haiqbot:onebot-sse"
        else:
            runner, name = self._run_client, "haiqbot:onebot-client"
        self._task = asyncio.create_task(runner(), name=name)
        self.mark_started()
        log.info("onebot_adapter_started", mode=self.cfg.mode)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        for task in list(self._event_tasks):
            task.cancel()
        self._event_tasks.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
            self._uvicorn = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._http = None

    async def _run_client(self) -> None:
        headers = {}
        if token := self._token_provider():
            headers["Authorization"] = f"Bearer {token}"
        backoff = 1.0
        while True:
            try:
                async with ws_connect(self.cfg.url, additional_headers=headers) as ws:
                    self._ws = ws
                    self._connected.set()
                    self._connected_since = time.time()
                    self._last_error = None
                    backoff = 1.0
                    log.info("onebot_connected", url=self.cfg.url)
                    await self._recv_loop(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected.clear()
                self._connected_since = None
                self._reconnects += 1
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("onebot_reconnect", error=type(exc).__name__, backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_server(self) -> None:
        expected = self._token_provider()

        async def handler(ws: Any) -> None:
            header = ""
            try:
                header = ws.request.headers.get("Authorization", "")
            except Exception:  # noqa: BLE001
                header = ""
            if expected and header != f"Bearer {expected}":
                log.warning("onebot_unauthorized", remote=getattr(ws, "remote_address", None))
                await ws.close(code=1008, reason="unauthorized")
                return
            self._ws = ws
            self._connected.set()
            self._connected_since = time.time()
            self._last_error = None
            log.info("onebot_client_joined")
            try:
                await self._recv_loop(ws)
            finally:
                self._connected.clear()
                self._connected_since = None
                if self._ws is ws:
                    self._ws = None

        self._server = await ws_serve(handler, self.cfg.host, self.cfg.port)
        log.info("onebot_server_listening", host=self.cfg.host, port=self.cfg.port)
        await self._server.wait_closed()

    # -- HTTP / SSE -------------------------------------------------------- #
    def _client(self) -> Any:
        """懒建 httpx 客户端。

        `trust_env=False` 是硬要求：本机代理（10808 之类）会把 127.0.0.1 的
        请求也劫走，表现成"协议端连不上"，排查起来很浪费时间。
        """
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(trust_env=False)
        return self._http

    def _auth_headers(self) -> dict[str, str]:
        token = self._token_provider()
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _call_http(
        self, action: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        """正向 HTTP：POST `{http_api_url}/{action}`，回包就是标准 OneBot 响应体。"""
        import httpx

        url = f"{self.cfg.http_api_url.rstrip('/')}/{action}"
        limit = timeout if timeout is not None else self.cfg.api_timeout_seconds
        try:
            resp = await self._client().post(
                url, json=params or {}, headers=self._auth_headers(), timeout=limit
            )
        except httpx.HTTPError as exc:
            self._connected.clear()
            self._connected_since = None
            self._last_error = f"{type(exc).__name__}: {exc}"[:200]
            raise AdapterError(f"OneBot HTTP 调用失败：{action}（{exc}）") from exc
        if resp.status_code >= 400:
            self._last_error = f"HTTP {resp.status_code}: {action}"
            raise AdapterError(f"OneBot HTTP {resp.status_code}：{action}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"OneBot HTTP 回包不是 JSON：{action}") from exc
        if not self._connected.is_set():
            self._connected.set()
            self._connected_since = time.time()
        self._last_error = None
        return _check_response(action, payload)

    def _webhook_secret(self) -> str | None:
        """HMAC 密钥：优先 webhook_secret_ref，否则用 access_token。

        真机（NapCat 4.18）实测：HTTP 上报的 `X-Signature` 就是用配置里的
        `token` 对原始 body 做 HMAC-SHA1，没有独立的 secret 字段。
        所以这里必须能回落到 token，否则合法上报会被一律 401。
        """
        ref = (self.cfg.webhook_secret_ref or "").strip()
        if ref and self._secret_provider is not None:
            value = self._secret_provider(ref)
            if value:
                return value
        return self._token_provider()

    def _verify_webhook(self, headers: Any, body: bytes) -> bool:
        """上报鉴权。

        1. 带了 `X-Signature: sha1=…` → 用 HMAC-SHA1 校验（NapCat / go-cqhttp 的做法）；
        2. 没带签名但配了 token → 要求 `Authorization: Bearer <token>`；
        3. 两者都没配 → 放行（启动时会告警）。
        """
        got = str(headers.get("x-signature") or "")
        secret = self._webhook_secret()
        if got:
            if not secret:
                return False
            want = "sha1=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
            return hmac.compare_digest(got, want)
        expected = self._token_provider()
        if not expected:
            return True
        return str(headers.get("authorization") or "") == f"Bearer {expected}"

    async def _ingest(self, payload: dict[str, Any]) -> None:
        """WS 之外的第二条入口：HTTP 上报与 SSE 都从这里进。

        回包（带 echo）优先解析成 pending 的 future，其余当事件异步派发 ——
        绝不能在这里 await 处理，否则「处理事件要调 API 等回包」会自锁。
        """
        echo = payload.get("echo")
        if echo is not None and echo in self._pending:
            fut = self._pending.pop(echo)
            if not fut.done():
                fut.set_result(payload)
            return
        task = asyncio.create_task(self._dispatch(payload), name="haiqbot:onebot-event")
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    def _build_webhook_app(self) -> Any:
        """上报端点做成独立 Starlette app —— 测试可以直接拿 TestClient 打。"""
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        async def endpoint(request: Request) -> Response:
            body = await request.body()
            if not self._verify_webhook(request.headers, body):
                log.warning("onebot_webhook_unauthorized", remote=str(request.client))
                return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
            try:
                payload = json.loads(body or b"{}")
            except Exception:  # noqa: BLE001
                return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)
            if not isinstance(payload, dict):
                return JSONResponse({"ok": False, "error": "bad payload"}, status_code=400)
            await self._ingest(payload)
            # 立刻 204：上报端等久了会重试，重试 = 重复事件
            return Response(status_code=204)

        return Starlette(routes=[Route(self.cfg.webhook_path, endpoint, methods=["POST"])])

    async def _run_webhook(self) -> None:
        import uvicorn

        if not self._token_provider() and not self._webhook_secret():
            log.warning(
                "onebot_webhook_unauthenticated",
                hint="没配 token 也没配 webhook_secret_ref：任何人推到该端口的事件都会被当真",
            )
        config = uvicorn.Config(
            self._build_webhook_app(),
            host=self.cfg.host,
            port=self.cfg.port,
            log_level="warning",
            access_log=False,
        )
        self._uvicorn = uvicorn.Server(config)
        self._connected.set()
        self._connected_since = time.time()
        log.info(
            "onebot_webhook_listening",
            host=self.cfg.host,
            port=self.cfg.port,
            path=self.cfg.webhook_path,
            api=self.cfg.http_api_url,
        )
        await self._uvicorn.serve()

    async def _run_sse(self) -> None:
        """正向 HTTP + SSE：事件流式读，**不需要任何入站端口**（适合 NAT 后）。"""
        # SSE 端口可以和 HTTP API 不同（NapCat 默认 3003 vs 3000），所以优先用完整地址
        url = self.cfg.sse_url or f"{self.cfg.http_api_url.rstrip('/')}{self.cfg.sse_path}"
        backoff = 1.0
        while True:
            try:
                async with self._client().stream(
                    "GET", url, headers=self._auth_headers(), timeout=None
                ) as resp:
                    if resp.status_code >= 400:
                        raise AdapterError(f"SSE HTTP {resp.status_code}")
                    self._connected.set()
                    self._connected_since = time.time()
                    self._last_error = None
                    backoff = 1.0
                    log.info("onebot_sse_connected", url=url)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if not chunk:
                            continue
                        try:
                            payload = json.loads(chunk)
                        except Exception:  # noqa: BLE001
                            continue
                        if isinstance(payload, dict):
                            await self._ingest(payload)
                # 流正常结束（协议端重启 / 滚动）也必须退避，
                # 否则就是不停重连的热循环，把协议端和自己都拖死。
                self._connected.clear()
                self._connected_since = None
                self._reconnects += 1
                log.warning("onebot_sse_closed", backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected.clear()
                self._connected_since = None
                self._reconnects += 1
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("onebot_sse_reconnect", error=type(exc).__name__, backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    # -- 收 --------------------------------------------------------------- #
    async def _recv_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                payload = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            echo = payload.get("echo")
            if echo is not None and echo in self._pending:
                fut = self._pending.pop(echo)
                if not fut.done():
                    fut.set_result(payload)
                continue
            # 关键：事件不能在这里 await。处理事件时可能要调 send_*_msg 并等回包，
            # 而回包只能由本循环读取 —— inline await 会直接死锁到 api_timeout。
            task = asyncio.create_task(self._dispatch(payload), name="haiqbot:onebot-event")
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    async def _dispatch(self, payload: dict[str, Any]) -> None:
        async with self._event_slots:
            try:
                await self._handle_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单个事件出错不能影响收包
                log.exception("onebot_event_error")

    async def _handle_event(self, payload: dict[str, Any]) -> None:
        """按 post_type 分流。**只有 message / notice / request / meta_event 进管线。**

        message_sent（自己发出去的消息）刻意忽略 —— 否则插件可能对自己回话。
        """
        post_type = payload.get("post_type")
        if post_type == "message":
            event = self._parse_message(payload)
        elif post_type == "notice":
            event = self._parse_notice(payload)
        elif post_type == "request":
            event = self._parse_request(payload)
        elif post_type == "meta_event":
            event = self._parse_meta(payload)
        else:
            return
        if event is not None:
            await self._emit(event)

    # -- 解析 ------------------------------------------------------------- #
    def _parse_message(self, payload: dict[str, Any]) -> MessageEvent | None:
        msg_type = payload.get("message_type")
        if msg_type == "group":
            channel = ChannelId("onebot", ChannelKind.GROUP, str(payload.get("group_id")))
        elif msg_type == "private":
            channel = ChannelId("onebot", ChannelKind.PRIVATE, str(payload.get("user_id")))
        else:
            return None
        sender = payload.get("sender") or {}
        # 完整保留原始上报（raw_message / sender.role / anonymous / message_format …）
        # 插件要判断"发言者是不是管理员"、要拿匿名信息，全靠它。
        raw: dict[str, Any] = dict(payload)
        segments = _decode_segments(payload.get("message"))
        bot_id = str(payload.get("self_id")) if payload.get("self_id") else None
        # @ 机器人：扫消息段里有没有指向自己的 at
        at_bot = any(
            seg.kind is SegmentKind.AT and bot_id is not None and str(seg.data.get("user_id")) == bot_id
            for seg in segments
        )
        return MessageEvent(
            kind=EventKind.MESSAGE,
            channel=channel,
            actor=Actor(
                id=str(payload.get("user_id")),
                name=sender.get("nickname"),
                role=_SENDER_ROLE.get(str(sender.get("role") or "member").lower(), Role.USER),
            ),
            segments=segments,
            msg_id=str(payload.get("message_id", "")),
            at_bot=at_bot,
            bot_id=bot_id,
            window_ttl=None,
            raw=raw,
        )

    def _parse_notice(self, payload: dict[str, Any]) -> NoticeEvent | None:
        notice_type = str(payload.get("notice_type") or "")
        sub_type = str(payload.get("sub_type") or "")
        group_id = payload.get("group_id")
        user_id = payload.get("user_id")
        operator_id = payload.get("operator_id")
        sender_id = payload.get("sender_id")

        # 群通知：频道是群；私聊/好友通知：频道是那次私聊
        if group_id is not None:
            channel = ChannelId("onebot", ChannelKind.GROUP, str(group_id))
        elif user_id is not None:
            channel = ChannelId("onebot", ChannelKind.PRIVATE, str(user_id))
        else:
            return None

        # 撤回：notice_id 借用 message_id（插件最关心的就是"哪条消息没了"）
        related = payload.get("message_id")
        event = NoticeEvent(
            kind=EventKind.NOTICE,
            channel=channel,
            actor=Actor(id=str(user_id) if user_id is not None else ""),
            notice_type=notice_type,
            sub_type=sub_type,
            notice_id=str(related) if related is not None else "",
            operator_id=str(operator_id) if operator_id is not None else None,
            target_id=str(payload.get("target_id")) if payload.get("target_id") is not None else None,
            raw=payload,
        )
        # poke 类：sender_id 才是"谁戳的"，user_id/target_id 是被戳的对象
        if sender_id is not None and operator_id is None:
            event.operator_id = str(sender_id)
        return event

    def _parse_request(self, payload: dict[str, Any]) -> RequestEvent | None:
        request_type = str(payload.get("request_type") or "")
        user_id = payload.get("user_id")
        if user_id is None:
            return None
        group_id = payload.get("group_id")
        channel = (
            ChannelId("onebot", ChannelKind.GROUP, str(group_id))
            if group_id is not None
            else ChannelId("onebot", ChannelKind.PRIVATE, str(user_id))
        )
        return RequestEvent(
            kind=EventKind.REQUEST,
            channel=channel,
            actor=Actor(id=str(user_id)),
            request_type=request_type,
            sub_type=str(payload.get("sub_type") or ""),
            flag=str(payload.get("flag") or ""),
            comment=str(payload.get("comment") or ""),
            group_id=str(group_id) if group_id is not None else None,
            raw=payload,
        )

    def _parse_meta(self, payload: dict[str, Any]) -> Event | None:
        """只放 lifecycle（连上/启用/停用）进管线；heartbeat 每 30s 一条，纯噪音。"""
        meta_type = payload.get("meta_event_type")
        if meta_type != "lifecycle":
            if payload.get("self_id"):
                self.bot_id = str(payload.get("self_id"))
            return None
        return Event(
            kind=EventKind.META,
            channel=ChannelId("onebot", ChannelKind.UNKNOWN, ""),
            actor=Actor(id=str(payload.get("self_id") or "")),
            raw=payload,
        )

    # -- 发 --------------------------------------------------------------- #
    async def call(
        self, action: str, params: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        if self.cfg.mode in ("http", "sse"):
            return await self._call_http(action, params, timeout=timeout)
        if self._ws is None:
            raise AdapterError("OneBot 未连接")
        self._echo += 1
        echo = f"haiqbot-{self._echo}"
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        await self._ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
        limit = timeout if timeout is not None else self.cfg.api_timeout_seconds
        try:
            resp = await asyncio.wait_for(fut, timeout=limit)
        except TimeoutError as exc:
            self._pending.pop(echo, None)
            raise AdapterError(f"OneBot 调用超时：{action}（{limit:.0f}s）") from exc
        return _check_response(action, resp)

    async def send_message(
        self,
        channel: ChannelId,
        segments: list[Segment],
        *,
        reply_to: str | None = None,
        seq: int | None = None,
        plugin_id: str | None = None,
    ) -> str | None:
        # 出站一律过闸门：同群串行 + 最小间隔 + 全局限速 + 频控退避
        return await self._guarded_send(
            channel,
            lambda: self._raw_send(channel, segments, reply_to=reply_to),
            plugin_id=plugin_id,
        )

    async def _raw_send(
        self,
        channel: ChannelId,
        segments: list[Segment],
        *,
        reply_to: str | None = None,
    ) -> str | None:
        # 合并转发要单独一个 action，不能混在普通消息里
        for seg in segments:
            if seg.kind is SegmentKind.FORWARD:
                return await self._send_forward_raw(channel, seg.data.get("nodes") or [])
        # 注意：seq 不参与 OneBot 编码（那是 QQ 官方平台的概念）
        message = _encode_segments(
            segments,
            reply_to,
            transport=self.cfg.image_transport,
            max_bytes=self.cfg.image_max_bytes,
        )
        # 带图/文件的发送要等协议端上传完，不能用控制类 API 的短超时
        has_media = any(s.kind in (SegmentKind.IMAGE, SegmentKind.FILE) for s in segments)
        extra = {"timeout": self.cfg.send_timeout_seconds} if has_media else {}

        if channel.kind is ChannelKind.GROUP:
            resp = await self.call(
                "send_group_msg",
                {"group_id": int(channel.id), "message": message},
                **extra,
            )
        elif channel.kind is ChannelKind.PRIVATE:
            resp = await self.call(
                "send_private_msg",
                {"user_id": int(channel.id), "message": message},
                **extra,
            )
        else:
            raise AdapterError(f"OneBot 适配器不支持该频道类型：{channel.kind}")
        if resp.get("status") == "failed":
            raise AdapterError(f"OneBot 发送失败：{resp.get('retcode')}")
        data = resp.get("data") or {}
        return str(data.get("message_id")) if data.get("message_id") is not None else None

    # -- 管理动作 --------------------------------------------------------- #
    # 全部走 `_guarded_send`：这些调用在 QQ 侧同样受频率限制，
    # 连发一串禁言和连发一串消息是同一种风险。
    async def recall(self, channel: ChannelId, message_id: str) -> bool:
        if not message_id:
            raise AdapterError("撤回需要 message_id")

        async def do() -> bool:
            await self.call("delete_msg", {"message_id": _as_id(message_id)})
            return True

        return await self._guarded_send(channel, do)

    async def mute(self, channel: ChannelId, user_id: str, seconds: int) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能禁言")
        duration = max(0, int(seconds))

        async def do() -> bool:
            await self.call(
                "set_group_ban",
                {
                    "group_id": _as_id(channel.id),
                    "user_id": _as_id(user_id),
                    "duration": duration,
                },
            )
            return True

        return await self._guarded_send(channel, do)

    async def kick(self, channel: ChannelId, user_id: str, *, reject_add_request: bool = False) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能踢人")

        async def do() -> bool:
            await self.call(
                "set_group_kick",
                {
                    "group_id": _as_id(channel.id),
                    "user_id": _as_id(user_id),
                    "reject_add_request": bool(reject_add_request),
                },
            )
            return True

        return await self._guarded_send(channel, do)

    async def send_forward(
        self, channel: ChannelId, nodes: list[dict[str, Any]], *, plugin_id: str | None = None
    ) -> str | None:
        return await self._guarded_send(
            channel,
            lambda: self._send_forward_raw(channel, nodes),
            plugin_id=plugin_id,
        )

    async def _send_forward_raw(self, channel: ChannelId, nodes: list[dict[str, Any]]) -> str | None:
        """`send_group_forward_msg` / `send_private_forward_msg`。

        每个节点必须包成 `{"type": "node", "data": {...}}` —— ForwardNode 只给
        data 部分，包装是协议要求。
        """
        if not nodes:
            raise AdapterError("合并转发至少需要一个节点")
        messages = [{"type": "node", "data": _node_data(node)} for node in nodes]
        if channel.kind is ChannelKind.GROUP:
            resp = await self.call(
                "send_group_forward_msg",
                {"group_id": _as_id(channel.id), "messages": messages},
                timeout=self.cfg.send_timeout_seconds,
            )
        elif channel.kind is ChannelKind.PRIVATE:
            resp = await self.call(
                "send_private_forward_msg",
                {"user_id": _as_id(channel.id), "messages": messages},
                timeout=self.cfg.send_timeout_seconds,
            )
        else:
            raise AdapterError(f"OneBot 适配器不支持该频道类型：{channel.kind}")
        if resp.get("status") == "failed":
            raise AdapterError(f"OneBot 合并转发失败：{resp.get('retcode')}")
        data = resp.get("data") or {}
        return str(data.get("message_id")) if data.get("message_id") is not None else None

    async def member_list(self, channel: ChannelId, *, limit: int = 100) -> list[dict[str, Any]]:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能读成员列表")
        resp = await self.call("get_group_member_list", {"group_id": _as_id(channel.id)})
        data = resp.get("data") or []
        out: list[dict[str, Any]] = []
        for row in data[: max(0, int(limit))]:
            out.append(
                {
                    "user_id": str(row.get("user_id")),
                    # 群名片优先 —— 群里显示的就是它
                    "name": row.get("card") or row.get("nickname") or "",
                    "role": row.get("role") or "member",
                }
            )
        return out

    # -- 查询（不走发送闸门：只读，不影响频控） -------------------------- #
    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        data = await self.call("get_msg", {"message_id": _as_id(message_id)})
        return (data or {}).get("data")

    async def get_forward(self, message_id: str) -> list[dict[str, Any]]:
        data = await self.call("get_forward_msg", {"message_id": message_id})
        payload = (data or {}).get("data") or {}
        nodes = payload.get("messages") or payload.get("message") or []
        return list(nodes) if isinstance(nodes, list) else []

    async def group_list(self) -> list[dict[str, Any]]:
        data = await self.call("get_group_list", {})
        payload = (data or {}).get("data") or []
        return list(payload) if isinstance(payload, list) else []

    async def group_info(self, channel: ChannelId) -> dict[str, Any] | None:
        data = await self.call("get_group_info", {"group_id": _as_id(channel.id)})
        return (data or {}).get("data")

    async def member_info(self, channel: ChannelId, user_id: str) -> dict[str, Any] | None:
        data = await self.call(
            "get_group_member_info",
            {
                "group_id": _as_id(channel.id),
                "user_id": _as_id(user_id),
                "no_cache": False,
            },
        )
        return (data or {}).get("data")

    async def stranger_info(self, user_id: str) -> dict[str, Any] | None:
        data = await self.call("get_stranger_info", {"user_id": _as_id(user_id)})
        return (data or {}).get("data")

    # -- 机器人自身 / 好友 / 精华 ---------------------------------------- #
    async def login_info(self) -> dict[str, Any] | None:
        data = await self.call("get_login_info", {})
        return (data or {}).get("data")

    async def status(self) -> dict[str, Any] | None:
        data = await self.call("get_status", {})
        return (data or {}).get("data")

    async def friend_list(self) -> list[dict[str, Any]]:
        data = await self.call("get_friend_list", {})
        payload = (data or {}).get("data") or []
        return list(payload) if isinstance(payload, list) else []

    async def essence_list(self, channel: ChannelId) -> list[dict[str, Any]]:
        data = await self.call("get_essence_msg_list", {"group_id": _as_id(channel.id)})
        payload = (data or {}).get("data") or []
        return list(payload) if isinstance(payload, list) else []

    async def version_info(self) -> dict[str, Any] | None:
        data = await self.call("get_version_info", {})
        return (data or {}).get("data")

    async def at_all_remain(self, channel: ChannelId) -> dict[str, Any] | None:
        data = await self.call("get_group_at_all_remain", {"group_id": _as_id(channel.id)})
        return (data or {}).get("data")

    # OneBot get_group_honor_info 的合法 type 值
    _HONOR_KINDS = frozenset({"all", "legend", "strong_newbie", "emotion"})

    async def honor_list(self, channel: ChannelId, kind: str = "all") -> dict[str, Any] | None:
        if kind not in self._HONOR_KINDS:
            raise AdapterError(f"不认识的荣誉类型：{kind}")
        data = await self.call(
            "get_group_honor_info",
            {"group_id": _as_id(channel.id), "type": kind},
        )
        return (data or {}).get("data")

    async def fetch_image(self, file_id: str) -> dict[str, Any] | None:
        """取图片文件。返回的 file 是**协议端本机路径**，容器部署要配共享卷。"""
        data = await self.call("get_image", {"file": file_id})
        return (data or {}).get("data")

    async def fetch_record(self, file_id: str) -> dict[str, Any] | None:
        data = await self.call("get_record", {"file": file_id, "out_format": "mp3"})
        return (data or {}).get("data")

    # -- 写操作（和发消息一样过出站闸门） -------------------------------- #
    async def set_group_card(self, channel: ChannelId, user_id: str, card: str) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能改群名片")

        async def do() -> bool:
            await self.call(
                "set_group_card",
                {
                    "group_id": _as_id(channel.id),
                    "user_id": _as_id(user_id),
                    "card": card,
                },
            )
            return True

        return await self._guarded_send(channel, do)

    async def upload_group_file(self, channel: ChannelId, path: str, name: str) -> str | None:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能上传文件")

        async def do() -> str | None:
            data = await self.call(
                "upload_group_file",
                {"group_id": _as_id(channel.id), "file": path, "name": name},
            )
            payload = (data or {}).get("data") or {}
            return payload.get("file_id") if isinstance(payload, dict) else None

        return await self._guarded_send(channel, do)

    async def set_group_name(self, channel: ChannelId, name: str) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能改群名")

        async def do() -> bool:
            await self.call(
                "set_group_name",
                {"group_id": _as_id(channel.id), "group_name": name},
            )
            return True

        return await self._guarded_send(channel, do)

    async def leave_group(self, channel: ChannelId) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能退群")

        async def do() -> bool:
            await self.call("set_group_leave", {"group_id": _as_id(channel.id)})
            return True

        return await self._guarded_send(channel, do)

    async def set_group_whole_ban(self, channel: ChannelId, enable: bool) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能全员禁言")

        async def do() -> bool:
            await self.call(
                "set_group_whole_ban",
                {"group_id": _as_id(channel.id), "enable": bool(enable)},
            )
            return True

        return await self._guarded_send(channel, do)

    async def send_like(self, user_id: str, times: int = 1) -> bool:
        # 协议端自带每日上限（通常 10 次），这里再夹一次防手滑
        count = max(1, min(int(times), 10))
        await self.call("send_like", {"user_id": _as_id(user_id), "times": count})
        return True

    async def mark_read(self, message_id: str) -> bool:
        await self.call("mark_msg_as_read", {"message_id": _as_id(message_id)})
        return True

    async def set_essence(self, message_id: str) -> bool:
        await self.call("set_essence_msg", {"message_id": _as_id(message_id)})
        return True

    async def del_essence(self, message_id: str) -> bool:
        await self.call("delete_essence_msg", {"message_id": _as_id(message_id)})
        return True

    async def set_admin(self, channel: ChannelId, user_id: str, enable: bool) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能设管理员")

        async def do() -> bool:
            await self.call(
                "set_group_admin",
                {
                    "group_id": _as_id(channel.id),
                    "user_id": _as_id(user_id),
                    "enable": bool(enable),
                },
            )
            return True

        return await self._guarded_send(channel, do)

    async def set_title(self, channel: ChannelId, user_id: str, title: str) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能设头衔")

        async def do() -> bool:
            await self.call(
                "set_group_special_title",
                {
                    "group_id": _as_id(channel.id),
                    "user_id": _as_id(user_id),
                    "special_title": title,
                },
            )
            return True

        return await self._guarded_send(channel, do)

    # -- 群管理补充 ---------------------------------------------------------- #
    async def set_portrait(self, channel: ChannelId, path: str) -> bool:
        """改群头像。`path` 是**协议端能读到的**路径或 URL（不是宿主机路径）。"""
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能改群头像")

        async def do() -> bool:
            await self.call(
                "set_group_portrait",
                {"group_id": _as_id(channel.id), "file": path},
            )
            return True

        return await self._guarded_send(channel, do)

    async def set_anonymous(self, channel: ChannelId, enable: bool) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊有匿名设置")

        async def do() -> bool:
            await self.call(
                "set_group_anonymous",
                {"group_id": _as_id(channel.id), "enable": bool(enable)},
            )
            return True

        return await self._guarded_send(channel, do)

    async def ban_anonymous(self, channel: ChannelId, flag: str, seconds: int = 60) -> bool:
        """禁言某个匿名发言者。`flag` 来自事件里的 `anonymous.flag`。"""
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能禁言匿名成员")
        if not flag:
            raise AdapterError("缺少 anonymous flag")

        async def do() -> bool:
            await self.call(
                "set_group_anonymous_ban",
                {
                    "group_id": _as_id(channel.id),
                    "anonymous_flag": flag,
                    "duration": int(seconds),
                },
            )
            return True

        return await self._guarded_send(channel, do)

    # -- 账号 / 社交 --------------------------------------------------------- #
    async def user_status(self, user_id: str) -> dict[str, Any] | None:
        """扩展动作。NapCat 4.18 实测 retcode=1404（不支持的 API）→ 可能抛 AdapterError。

        `actions` 表达的是「本适配器实现了」，不代表每个协议端都实现；
        调用方要么 catch AdapterError，要么自己缓存第一次的结果。
        """
        data = await self.call("get_user_status", {"user_id": _as_id(user_id)})
        return (data or {}).get("data")

    async def unidirectional_friends(self) -> list[dict[str, Any]]:
        data = await self.call("get_unidirectional_friend_list", {})
        payload = (data or {}).get("data") or []
        return list(payload) if isinstance(payload, list) else []

    async def sign_info(self, channel: ChannelId) -> dict[str, Any] | None:
        """扩展动作（go-cqhttp 遗留）。NapCat 4.18 实测 retcode=1404 → 可能抛 AdapterError。"""
        data = await self.call("get_group_sign", {"group_id": _as_id(channel.id)})
        return (data or {}).get("data")

    async def sign_in(self, channel: ChannelId) -> bool:
        """真的打卡（会改群签到状态）—— 由 PlatformCap 记审计。

        扩展动作：NapCat 4.18 实测 retcode=1404 → 可能抛 AdapterError。
        """
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能签到")
        await self.call("send_group_sign", {"group_id": _as_id(channel.id)})
        return True

    async def can_send_image(self, channel: ChannelId | None = None) -> bool:
        """协议端当前能不能发图。`channel` 可选：群聊会带上 group_id 再问一次。"""
        params: dict[str, Any] = {}
        if channel is not None and channel.kind is ChannelKind.GROUP:
            params["group_id"] = _as_id(channel.id)
        data = await self.call("can_send_image", params)
        payload = (data or {}).get("data") or {}
        return bool(payload.get("yes")) if isinstance(payload, dict) else bool(payload)

    async def can_send_record(self, channel: ChannelId | None = None) -> bool:
        params: dict[str, Any] = {}
        if channel is not None and channel.kind is ChannelKind.GROUP:
            params["group_id"] = _as_id(channel.id)
        data = await self.call("can_send_record", params)
        payload = (data or {}).get("data") or {}
        return bool(payload.get("yes")) if isinstance(payload, dict) else bool(payload)

    async def system_messages(self, channel: ChannelId) -> dict[str, Any] | None:
        """加群请求 / 被邀请记录。扩展动作，协议端不实现时抛 AdapterError。"""
        data = await self.call("get_group_system_msg", {"group_id": _as_id(channel.id)})
        return (data or {}).get("data")

    # -- 群文件系统 ---------------------------------------------------------- #
    @staticmethod
    def _file_tree(data: Any) -> dict[str, list[dict[str, Any]]]:
        """归一成 `{files, folders}`：保留协议端原始字段，另加 `id`/`name`/`size`。

        不发明平台 schema：`file_id`/`file_name`/`busid` 等原样留着，
        插件按需取；只要简短标识时用 `id`/`name`。
        """
        payload = data if isinstance(data, dict) else {}
        files: list[dict[str, Any]] = []
        for row in payload.get("files") or []:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("id", str(row.get("file_id") or ""))
            item.setdefault("name", str(row.get("file_name") or ""))
            item.setdefault("size", int(row.get("file_size") or 0))
            files.append(item)
        folders: list[dict[str, Any]] = []
        for row in payload.get("folders") or []:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item.setdefault("id", str(row.get("folder_id") or ""))
            item.setdefault("name", str(row.get("folder_name") or ""))
            item.setdefault("count", int(row.get("total_file_count") or 0))
            folders.append(item)
        return {"files": files, "folders": folders}

    async def group_files(self, channel: ChannelId) -> dict[str, list[dict[str, Any]]]:
        data = await self.call("get_group_root_files", {"group_id": _as_id(channel.id)})
        return self._file_tree((data or {}).get("data"))

    async def folder_files(self, channel: ChannelId, folder_id: str) -> dict[str, list[dict[str, Any]]]:
        data = await self.call(
            "get_group_files_by_folder",
            {"group_id": _as_id(channel.id), "folder_id": folder_id},
        )
        return self._file_tree((data or {}).get("data"))

    async def file_url(self, channel: ChannelId, file_id: str, busid: int) -> str | None:
        data = await self.call(
            "get_group_file_url",
            {"group_id": _as_id(channel.id), "file_id": file_id, "busid": int(busid)},
        )
        payload = (data or {}).get("data") or {}
        return payload.get("url") if isinstance(payload, dict) else None

    async def file_system_info(self, channel: ChannelId) -> dict[str, Any] | None:
        data = await self.call("get_group_file_system_info", {"group_id": _as_id(channel.id)})
        return (data or {}).get("data")

    async def create_folder(self, channel: ChannelId, name: str, parent_id: str = "") -> str | None:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能建文件夹")

        async def do() -> str | None:
            data = await self.call(
                "create_group_file_folder",
                {
                    "group_id": _as_id(channel.id),
                    "name": name,
                    "parent_id": parent_id or "/",
                },
            )
            payload = (data or {}).get("data") or {}
            if isinstance(payload, dict):
                return payload.get("folder_id") or payload.get("id")
            return None

        return await self._guarded_send(channel, do)

    async def delete_file(self, channel: ChannelId, file_id: str, busid: int) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能删群文件")

        async def do() -> bool:
            await self.call(
                "delete_group_file",
                {
                    "group_id": _as_id(channel.id),
                    "file_id": file_id,
                    "busid": int(busid),
                },
            )
            return True

        return await self._guarded_send(channel, do)

    async def delete_folder(self, channel: ChannelId, folder_id: str) -> bool:
        if channel.kind is not ChannelKind.GROUP:
            raise AdapterError("只有群聊能删群文件夹")

        async def do() -> bool:
            params = {"group_id": _as_id(channel.id), "folder_id": folder_id}
            try:
                await self.call("delete_group_folder", params)
            except AdapterError:
                # 某些协议端只认老名字 delete_group_file_folder
                await self.call("delete_group_file_folder", params)
            return True

        return await self._guarded_send(channel, do)

    async def set_emoji_like(self, message_id: str, code: str) -> bool:
        await self.call("set_msg_emoji_like", {"message_id": _as_id(message_id), "emoji_id": code})
        return True

    async def handle_request(self, event: Any, approve: bool, *, reason: str = "") -> bool:
        flag = getattr(event, "flag", "") or ""
        if not flag:
            raise AdapterError("请求缺少 flag，无法审批")
        request_type = getattr(event, "request_type", "")
        if request_type == "friend":
            await self.call("set_friend_add_request", {"flag": flag, "approve": bool(approve)})
            return True
        if request_type == "group":
            payload: dict[str, Any] = {
                "flag": flag,
                # add = 主动申请入群，invite = 被邀请入群；OneBot 要求回传
                "sub_type": getattr(event, "sub_type", "") or "add",
                "approve": bool(approve),
            }
            if not approve and reason:
                payload["reason"] = reason
            await self.call("set_group_add_request", payload)
            return True
        raise AdapterError(f"不认识的请求类型：{request_type or '空'}")


# OneBot 的失败不走 HTTP 状态码：`status` / `retcode` 才是真相。
# retcode 0 = 成功；1 = 异步受理（部分实现）；其余一律视为失败。
_OK_RETCODES = (0, 1, None)


#: NapCat/QQ NT 有时会先报「发消息事件回执超时」，但消息其实已经进群了。
#: 媒体发送失败的两种典型签名（都来自 QQ NT，不是"我们等太久"）：
#:   Timeout: NTEvent ... sendMsg              -> 上传没在窗口内完成
#:   EventChecker Failed ... rich media ...    -> 上传被明确拒绝
_ACK_TIMEOUT_HINT = "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg"
_RICH_MEDIA_HINT = "rich media transfer failed"


def _is_media_send_failure(action: str, detail: Any) -> bool:
    """`send_*` 动作是否命中"媒体通道失败"的两种签名。

    实测（NapCat + QQ NT，Docker）：base64 / 本地路径 / file:// 发图都可能失败，且有两种表现。
    两者都必须归为 **媒体发送失败**：
    - 不能重试（重试只会加重平台限制，还会把 4×60s 拖成体感卡死）；
    - 让插件把「生成成功 / 发送失败」如实报出来，而不是静默丢图。
    """
    if not str(action).startswith("send_"):
        return False
    text = str(detail or "")
    return _ACK_TIMEOUT_HINT in text or _RICH_MEDIA_HINT in text


def _check_response(action: str, resp: Any) -> dict[str, Any]:
    """把协议端返回的失败转成异常，避免"假成功"。

    媒体失败是个特例：它不是普通错误，而是「图片没进群」，且**不能重试**。
    所以这里抛 `MediaSendError`，由插件把原因说清楚（见 painter 的生成/发送失败分报）。
    """
    if not isinstance(resp, dict):
        return {"status": "ok", "data": resp}
    status = str(resp.get("status") or "").lower()
    retcode = resp.get("retcode")
    if status == "failed" or retcode not in _OK_RETCODES:
        detail = resp.get("wording") or resp.get("message") or "调用失败"
        if _is_media_send_failure(action, detail):
            reason = (
                "平台拒绝了媒体上传（rich media transfer failed）"
                if _RICH_MEDIA_HINT in str(detail)
                else "协议端回执超时（媒体上传未完成）"
            )
            log.warning("onebot_media_rejected", action=action, reason=reason)
            raise MediaSendError(f"媒体消息未送达：{reason}")
        raise AdapterError(f"OneBot {action} 失败（retcode={retcode}）：{detail}")
    return resp


def _node_data(node: dict[str, Any]) -> dict[str, Any]:
    """把 `ForwardNode.to_dict()` 转成 OneBot 要的节点 data。

    关键点：节点里的 `content` 是**我们内部的 Segment 字典**（`kind` 键），
    OneBot 要的是 `type` 键的消息段数组。这里必须真的编码一遍，
    否则协议端会返回 retcode 1200（发送失败），而不是报"格式不对"。
    """
    data = dict(node)
    content = data.get("content")
    if isinstance(content, list):
        data["content"] = _encode_segments([Segment.from_dict(x) for x in content], None)
    return data


def _as_id(value: Any) -> Any:
    """OneBot 的 ID 字段有时要 int、有时要 str。数字就转 int，其余原样。"""
    text = str(value)
    return int(text) if text.isdigit() else text


# --------------------------------------------------------------------------- #
def _decode_segments(message: Any) -> list[Segment]:
    if isinstance(message, str):
        return [Segment.text(message)]
    out: list[Segment] = []
    for item in message or []:
        kind = item.get("type")
        data = item.get("data") or {}
        if kind == "text":
            out.append(Segment.text(str(data.get("text", ""))))
        elif kind == "image":
            out.append(Segment.image(url=str(data.get("url") or data.get("file") or "")))
        elif kind == "at":
            out.append(Segment.at(str(data.get("qq"))))
        elif kind == "reply":
            out.append(Segment.reply(str(data.get("id"))))
        elif kind == "face":
            out.append(Segment.face(data.get("id", "")))
        elif kind == "record":
            out.append(Segment.record(str(data.get("url") or data.get("file") or "")))
        elif kind == "video":
            out.append(Segment.video(str(data.get("url") or data.get("file") or "")))
        elif kind == "forward":
            # 合并转发消息段只给一个 id，内容要另外调 get_forward_msg 取
            out.append(Segment(SegmentKind.FORWARD, {"id": str(data.get("id") or "")}))
        else:
            # 未知段：类型 + 标量字段都留下来，正文抽得出来就不算丢
            kept: dict[str, Any] = {"raw_type": kind}
            for key, value in data.items():
                if isinstance(value, (str, int, float, bool)):
                    kept[key] = value
            out.append(Segment(SegmentKind.UNKNOWN, kept))
    return out


def _image_file(seg: Segment, transport: str, max_bytes: int) -> str:
    """决定 OneBot 的 `file` 字段：内嵌 base64 还是 URL。

    base64 是「直发」：图片字节跟着消息一起走，平台不需要能访问我们的服务器。
    """
    url = str(seg.data.get("url") or "")
    if transport == "url":
        return url

    path = seg.data.get("path")
    if not path:
        if transport == "base64":
            log.warning("image_embed_without_local_file", has_url=bool(url))
        return url

    file_path = Path(str(path))
    try:
        size = file_path.stat().st_size
        if size > max_bytes:
            log.warning("image_too_large_for_embed", bytes=size, limit=max_bytes)
            return url
        return "base64://" + base64.b64encode(file_path.read_bytes()).decode("ascii")
    except OSError as exc:
        log.warning("image_read_failed", error=type(exc).__name__, path=str(file_path.name))
        return url


def _encode_segments(
    segments: list[Segment],
    reply_to: str | None,
    *,
    transport: str = "auto",
    max_bytes: int = 20_000_000,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if reply_to:
        out.append({"type": "reply", "data": {"id": reply_to}})
    for seg in segments:
        if seg.kind is SegmentKind.TEXT:
            out.append({"type": "text", "data": {"text": str(seg.data.get("text", ""))}})
        elif seg.kind is SegmentKind.IMAGE:
            out.append({"type": "image", "data": {"file": _image_file(seg, transport, max_bytes)}})
        elif seg.kind is SegmentKind.AT:
            out.append({"type": "at", "data": {"qq": str(seg.data.get("user_id", ""))}})
        elif seg.kind is SegmentKind.AT_ALL:
            out.append({"type": "at", "data": {"qq": "all"}})
        elif seg.kind is SegmentKind.FILE:
            url = seg.data.get("url") or seg.data.get("path") or ""
            out.append({"type": "file", "data": {"file": url}})
        elif seg.kind is SegmentKind.FACE:
            out.append({"type": "face", "data": {"id": str(seg.data.get("id", ""))}})
        elif seg.kind in (SegmentKind.RECORD, SegmentKind.VIDEO):
            url = seg.data.get("url") or seg.data.get("path") or ""
            kind = "record" if seg.kind is SegmentKind.RECORD else "video"
            out.append({"type": kind, "data": {"file": url}})
        elif seg.kind is SegmentKind.MARKDOWN:
            # OneBot 没有 markdown 概念：降级成纯文本，别把内容丢了
            log.warning("onebot_markdown_degraded")
            out.append({"type": "text", "data": {"text": str(seg.data.get("text", ""))}})
        elif seg.kind is SegmentKind.KEYBOARD:
            # 按钮只有 QQ 官方支持；OneBot 侧明确丢弃并告警
            log.warning("onebot_keyboard_unsupported")
        elif seg.kind is SegmentKind.FORWARD:
            # 合并转发走独立 action（send_group_forward_msg），不在普通消息里编码
            log.warning("onebot_forward_should_use_send_forward")
    return out
