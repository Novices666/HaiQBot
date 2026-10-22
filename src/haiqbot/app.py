"""应用装配：把内核、服务、适配器、插件串起来。

这里是**唯一**知道 vault 存在的地方（除了 services/）。插件、适配器都只拿到凭据的结果，
不知道凭据从哪来。
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import uuid
from pathlib import Path
from typing import Any

import uvicorn

from haiqbot.adapters.base import Adapter
from haiqbot.adapters.mock import MockAdapter
from haiqbot.config import HaiqConfig, load_config
from haiqbot.core.event import Event, MessageEvent
from haiqbot.core.reply import Reply
from haiqbot.kernel.bus import Bus, PipelineContext
from haiqbot.kernel.channels import ChannelGate
from haiqbot.kernel.governance import AuditLog, Idempotency, QuotaService, RoleResolver
from haiqbot.kernel.log import configure_logging, get_logger
from haiqbot.kernel.outbound import OutboundLimiter
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
from haiqbot.kernel.store import Store
from haiqbot.kernel.vault import Vault
from haiqbot.services.media_store import MediaStore
from haiqbot.services.scheduler import Scheduler
from haiqbot.webui import build_webui

log = get_logger(__name__)

# 演示输出里的人话标签（只覆盖常见段类型，其余回落成 [kind]）
_OUT_SEG_LABELS = {
    "record": "语音",
    "video": "视频",
    "forward": "合并转发",
    "button": "按钮",
    "json": "卡片",
}


class App:
    def __init__(self, config: HaiqConfig | None = None) -> None:
        self.config = config or load_config()
        self.store: Store | None = None
        self.vault: Vault | None = None
        self.bus: Bus | None = None
        self.registry: Registry | None = None
        self.runtime: Runtime | None = None
        self.adapters: dict[str, Adapter] = {}
        self._server: uvicorn.Server | None = None
        self._webui_task: asyncio.Task[None] | None = None
        self._media_server: uvicorn.Server | None = None
        self._media_task: asyncio.Task[None] | None = None

    # -- 装配 ------------------------------------------------------------- #
    async def setup(self, *, demo: bool = False, enable_adapters: bool = True) -> App:
        cfg = self.config
        configure_logging(cfg.log_level, cfg.log_json)
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)

        if demo:
            # 演示模式：让提供方插件自己切到 mock（内核不认识 provider）
            cfg.plugin_config.setdefault("painter", {})["provider"] = "mock"
            cfg.plugin_config.setdefault("chat", {})["provider"] = "mock"

        self.store = await Store(cfg.db_path).open()
        self.vault = Vault.from_config(cfg)
        # 启动期探测一次凭据库：密码错 / 文件坏要在这里可见，
        # 而不是等群里第一条消息才炸出一个含糊的"生成失败"。
        try:
            self.vault.keys()
        except Exception as exc:  # noqa: BLE001
            log.error(
                "vault_unavailable",
                error=type(exc).__name__,
                hint="检查 WEFT_VAULT_PASSPHRASE 或执行 haiq secrets reencrypt",
            )

        resolver = RoleResolver.from_policy(cfg.policy)
        quota = QuotaService(self.store, cfg.quota, resolver)
        idem = Idempotency(self.store)
        audit = AuditLog(self.store)

        media = MediaStore(self.store, cfg.media, webui=cfg.webui)
        scheduler = Scheduler()
        # 出站闸门：所有平台发送都过它（防 QQ 风控）
        outbound = OutboundLimiter(cfg.outbound)
        # 群维度配置闸门（群静默 / 只放行部分插件 / 每群配额）
        channel_gate = ChannelGate(self.store)

        bus = Bus(timeout=cfg.policy.handle_timeout_seconds, max_handlers=cfg.policy.max_handlers_per_event)

        self.runtime = Runtime(
            config=cfg,
            store=self.store,
            vault=self.vault,
            bus=bus,
            resolver=resolver,
            quota=quota,
            idempotency=idem,
            audit=audit,
            media=media,
            scheduler=scheduler,
            outbound=outbound,
            channel_gate=channel_gate,
        )
        self.bus = bus

        # ⑤ 前置中间件（①②③④）—— trace 在 _on_event 里先注入，这里负责其余三步
        bus.add_middleware("dedupe", self._mw_dedupe, order=20)
        bus.add_middleware("scope", self._mw_scope, order=30)
        bus.add_middleware("quota", self._mw_quota, order=40)

        self.registry = Registry(
            self.runtime,
            storage_root=Path(cfg.data_dir) / "plugins",
            net_hosts=[],
        )

        if enable_adapters:
            await self._setup_adapters(demo=demo)
            # 适配器复用同一个闸门：跨平台也共享全局速率
            for adapter in self.adapters.values():
                adapter.outbound = outbound
            # 让 runtime 看到同一份适配器视图（仪表盘 /api/status 依赖它）
            self.runtime.adapters = self.adapters

        return self

    async def _setup_adapters(self, *, demo: bool) -> None:
        cfg = self.config
        if demo:
            mock = MockAdapter()
            mock.bind(lambda ev, a=mock: self._on_event(ev, a))
            self.adapters["mock"] = mock
            return

        if cfg.official.enabled:
            if not cfg.official.app_id:
                log.error("official_missing_app_id")
            else:
                from haiqbot.adapters.official import OfficialAdapter, OfficialClient

                assert self.vault is not None
                client = OfficialClient(
                    app_id=cfg.official.app_id,
                    secret_provider=lambda: self.vault.require(cfg.official.app_secret_ref),  # type: ignore[union-attr]
                    api_base=cfg.official.api_base,
                    token_url=cfg.official.token_url,
                )
                adapter = OfficialAdapter(client=client, intents=cfg.official.intents)
                adapter.bind(lambda ev, a=adapter: self._on_event(ev, a))
                self.adapters["official"] = adapter

        if cfg.onebot.enabled:
            from haiqbot.adapters.onebot11 import OneBotAdapter

            assert self.vault is not None
            adapter = OneBotAdapter(
                cfg.onebot,
                token_provider=lambda: self.vault.get(cfg.onebot.access_token_ref),  # type: ignore[union-attr]
                # HTTP 上报的 HMAC 校验用；WS 模式不会走到
                secret_provider=lambda ref: self.vault.get(ref),  # type: ignore[union-attr]
            )
            adapter.bind(lambda ev, a=adapter: self._on_event(ev, a))
            self.adapters["onebot"] = adapter

        if not self.adapters:
            log.warning("no_adapter_enabled", hint="在 haiq.toml 里启用 [official] 或 [onebot]")

    # -- 中间件 ----------------------------------------------------------- #
    async def _mw_dedupe(self, pctx: PipelineContext) -> None:
        assert self.runtime is not None
        event = pctx.event
        if isinstance(event, MessageEvent) and event.msg_id:
            key = f"{event.channel.platform}:{event.msg_id}"
            if not await self.runtime.idempotency.first_time(key):
                log.info("duplicate_message_dropped", msg_id=event.msg_id)
                pctx.stop("duplicate")

    async def _mw_scope(self, pctx: PipelineContext) -> None:
        assert self.runtime is not None
        policy = self.config.policy
        channel = pctx.event.channel
        if policy.channel_blacklist and any(channel.matches(p) for p in policy.channel_blacklist):
            pctx.stop("blacklisted")
            return
        if policy.channel_whitelist and not any(channel.matches(p) for p in policy.channel_whitelist):
            pctx.stop("not_whitelisted")
            return
        # 群维度（channel_config）：**默认全关**。
        # 没配置 / enabled=0 / 开启项为空 → 不响应该频道，事件到此为止。
        # 开关的唯一入口是面板与 CLI（内核不提供群内管理命令）。
        gate = self.runtime.channel_gate
        if gate is not None:
            key = str(channel)
            row = await gate.row(key)
            enabled = bool(row and row.get("enabled", 1))
            allowed = await gate.allowed_plugins(key) if enabled else set()
            closed = (not enabled) or (not allowed)
            # 不 stop：交给 Bus 按规则过滤，gating = "always" 的插件才能豁免
            pctx.extra["scope_closed"] = closed
            pctx.extra["allowed_plugins"] = allowed
            if closed:
                pctx.extra["scope_reason"] = (
                    "channel_not_configured"
                    if row is None
                    else "channel_disabled"
                    if not enabled
                    else "channel_no_plugins"
                )
        registry = getattr(self, "registry", None)
        if registry is not None:
            always = {
                pid
                for pid, handle in registry.handles().items()
                if getattr(getattr(handle, "plugin", None), "gating", "opt_in") == "always"
            }
            if always:
                pctx.extra["always_plugins"] = always
        role = self.runtime.resolver.resolve(pctx.event.actor.id, pctx.event.actor.role)
        if role != pctx.event.actor.role:
            pctx.event.actor = dataclasses.replace(pctx.event.actor, role=role)

    async def _mw_quota(self, pctx: PipelineContext) -> None:
        """闸门：判断是否超限/超速，**不扣费**。

        扣费挪到能力句柄里（`image.generate` / `llm.chat` 各记一次），
        这样一条消息触发两个昂贵动作就记两次。
        """
        assert self.runtime is not None
        if not isinstance(pctx.event, MessageEvent):
            return
        overrides = None
        if self.runtime.channel_gate is not None:
            overrides = await self.runtime.channel_gate.quota_overrides(str(pctx.event.channel))
        ok, reason = await self.runtime.quota.check(
            actor_id=pctx.event.actor.id, channel=pctx.event.channel, overrides=overrides
        )
        if not ok:
            await pctx.reply.text(reason)
            pctx.stop("quota")

    # -- 事件入口 --------------------------------------------------------- #
    async def _on_event(self, event: Event, adapter: Adapter) -> None:
        assert self.runtime is not None
        if not event.trace_id:
            event.trace_id = uuid.uuid4().hex[:12]

        caps = adapter.capabilities
        deadline = None
        if isinstance(event, MessageEvent) and caps.window_ttl:
            deadline = event.ts + caps.window_ttl

        reply = Reply(
            adapter,
            event.channel,
            capabilities=caps,
            source_msg_id=getattr(event, "msg_id", None) or None,
            deadline=deadline,
        )
        try:
            pctx = await self.bus.publish(event, reply)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 兜底，绝不让单个事件打死循环
            log.exception("pipeline_crashed", trace=event.trace_id)
            return

        action = "stopped" if pctx.stopped else ("handled" if pctx.handled else "unhandled")
        self.runtime.note_event(handled=pctx.handled)

        # 基费：handler 跑了但没碰昂贵能力（echo / help 这类）也要记一份，
        # 否则「按动作计费」会变成「白嫖」。
        if pctx.handled and isinstance(event, MessageEvent):
            if int(pctx.extra.get("charges", 0)) == 0:
                await self.runtime.quota.consume(actor_id=event.actor.id, channel=event.channel)

        await self.runtime.audit.record(
            trace_id=event.trace_id,
            plugin_id=pctx.plugin_id,
            action=f"{action}:{pctx.stop_reason}" if pctx.stopped else action,
            channel=event.channel,
            actor=event.actor.id,
        )

    # -- 生命周期 --------------------------------------------------------- #
    async def start_plugins(self) -> list[str]:
        assert self.registry is not None
        return await self.registry.load_enabled()

    async def start_adapters(self) -> None:
        for name, adapter in self.adapters.items():
            await adapter.start()
            log.info("adapter_started", adapter=name)
        if self.runtime and self.runtime.scheduler:
            await self.runtime.scheduler.start()
            self._register_core_jobs()

    def _register_core_jobs(self) -> None:
        """内核自己的周期性维护（目前只有媒体 GC）。业务定时任务归插件。"""
        runtime = self.runtime
        if runtime is None or getattr(runtime, "scheduler", None) is None:
            return
        media = getattr(runtime, "media", None)
        interval = float(getattr(self.config.media, "gc_interval_seconds", 0) or 0)
        if media is not None and interval > 0:
            runtime.scheduler.add_interval("core:media-gc", interval, media.gc)
            log.info("core_job_registered", job="core:media-gc", seconds=interval)

    async def start_webui(self) -> None:
        if not self.config.webui.enabled or self.runtime is None:
            return
        assert self.registry is not None
        app = build_webui(self.runtime, self.registry)
        config = uvicorn.Config(
            app,
            host=self.config.webui.host,
            port=self.config.webui.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._webui_task = asyncio.create_task(self._server.serve(), name="haiqbot:webui")
        log.info("webui_listening", host=self.config.webui.host, port=self.config.webui.port)

    async def start_media_http(self) -> None:
        """只读媒体服务：给协议端一个能拉到图片的 URL。

        实测 NapCat 里 base64 / 本地路径 / file:// 发图都会被拒（rich media
        transfer failed），只有 http(s) URL 能成。面板仍然只绑 127.0.0.1，
        这里单独开一个**只暴露 `/i/<id>`** 的端口，把暴露面压到最小。
        """
        cfg = self.config.media
        if self.runtime is None or getattr(self.runtime, "media", None) is None:
            return
        port = int(getattr(cfg, "serve_port", 0) or 0)
        if port <= 0:
            return
        from haiqbot.services.media_http import build_media_app

        host = str(getattr(cfg, "serve_host", "0.0.0.0") or "0.0.0.0")
        app = build_media_app(self.runtime.media)
        self._media_server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
        )
        self._media_task = asyncio.create_task(self._media_server.serve(), name="haiqbot:media-http")
        log.info("media_http_listening", host=host, port=port)

    async def run(self) -> None:
        await self.setup()
        await self.start_plugins()
        await self.start_adapters()
        await self.start_webui()
        await self.start_media_http()
        log.info(
            "haiqbot_ready",
            platforms=list(self.adapters),
            plugins=len(self.registry.handles() if self.registry else {}),
        )
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    async def shutdown(self) -> None:
        for adapter in self.adapters.values():
            with contextlib.suppress(Exception):
                await adapter.stop()
        if self.runtime and self.runtime.scheduler:
            await self.runtime.scheduler.stop()
        if self._server is not None:
            self._server.should_exit = True
            if self._webui_task:
                with contextlib.suppress(Exception):
                    await self._webui_task
        if self._media_server is not None:
            self._media_server.should_exit = True
            if self._media_task:
                with contextlib.suppress(Exception):
                    await self._media_task
        if self.registry is not None:
            for pid in list(self.registry.handles()):
                with contextlib.suppress(Exception):
                    await self.registry.unload(pid)
        if self.store is not None:
            await self.store.close()
        log.info("haiqbot_stopped")

    @staticmethod
    def _describe_segments(segments: object) -> str:
        """把出站段渲染成一眼能懂的一行（演示/干跑用）。

        原来直接 `str(segments)` 会把 dict 原样倒出来 —— 新用户第一眼看到
        `[{'kind': 'image', 'data': {...}}]`，等于没演示。
        """
        parts: list[str] = []
        for seg in segments or []:  # type: ignore[union-attr]
            # `sent` 里的段是 `Segment.to_dict()` 的结果（dict），不是 Segment 对象
            if isinstance(seg, dict):
                kind = seg.get("kind", "")
                data = seg.get("data") or {}
            else:
                kind = getattr(seg, "kind", "")
                data = getattr(seg, "data", {}) or {}
            name = getattr(kind, "value", str(kind))
            if name in ("text", "markdown"):
                parts.append(str(data.get("text", "")))
            elif name == "image":
                mid = str(data.get("media_id") or "")
                parts.append(f"[图片 {mid[:8]}]" if mid else "[图片]")
            elif name == "file":
                fname = data.get("name") or ""
                parts.append(f"[文件 {fname}]" if fname else "[文件]")
            elif name in _OUT_SEG_LABELS:
                parts.append(f"[{_OUT_SEG_LABELS[name]}]")
            elif name:
                parts.append(f"[{name}]")
        return " ".join(p for p in parts if p)

    # -- 演示 ------------------------------------------------------------- #
    async def demo(self) -> dict[str, Any]:
        """不连 QQ、不烧额度地跑一遍完整链路。"""
        # 演示要可重复：不吃真实日配额，也不把演示数据混进线上库 / 媒体目录。
        # （注意这只是 CLI 演示的隔离，`setup(demo=True)` 本身仍保留配额，
        #   否则测试里就没法验证配额闸门了。）
        self.config.quota.enabled = False
        self.config.db_path = Path(self.config.data_dir) / "haiqbot-demo.db"
        self.config.media.dir = Path(self.config.data_dir) / "media-demo"
        await self.setup(demo=True)
        await self.start_plugins()
        await self.start_adapters()

        mock: MockAdapter = self.adapters["mock"]  # type: ignore[assignment]
        from haiqbot.core.channel import Actor, ChannelId, ChannelKind
        from haiqbot.core.event import EventKind, MessageEvent
        from haiqbot.core.segment import Segment

        channel = ChannelId("mock", ChannelKind.GROUP, "10001")
        # 默认全关语义下，演示频道要显式开起来 —— 否则 demo 什么都不做，体验就是坏的
        from haiqbot.kernel import channel_control as _cc

        await _cc.set_features(
            self.runtime,
            self.registry,
            str(channel),
            replace=sorted(self.registry.handles()),
            actor="demo",
        )
        transcript: list[str] = []

        demo_inputs = [
            "/help",
            "/画图 一只在雪山顶上看日落的橘猫",
            "/问 用一句话介绍你自己",
            "/echo 你好 haiqbot",
        ]
        for index, text in enumerate(demo_inputs):
            before = len(mock.sent)
            run_id = uuid.uuid4().hex[:8]
            await mock.inject(
                MessageEvent(
                    kind=EventKind.MESSAGE,
                    channel=channel,
                    actor=Actor(id="demo-user", name="演示用户"),
                    segments=[Segment.text(text)],
                    msg_id=f"demo-{run_id}-{index}",
                    at_bot=True,
                    bot_id="mock-bot",
                    window_ttl=None,
                )
            )
            transcript.append(f"> {text}")
            for sent in mock.sent[before:]:
                detail = sent["text"] or self._describe_segments(sent["segments"])
                transcript.append(f"  ← {detail}")
        await self.shutdown()
        return {"transcript": transcript, "sent": mock.sent}
