"""运行时依赖容器 —— 内核各子系统在这里汇合。"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Any
from haiqbot.config import HaiqConfig
from haiqbot.kernel.bus import Bus
from haiqbot.kernel.governance import AuditLog, Idempotency, QuotaService, RoleResolver
from haiqbot.kernel.store import Store
from haiqbot.kernel.vault import Vault

@dataclass
class Runtime:
    config: HaiqConfig
    store: Store
    vault: Vault
    bus: Bus
    resolver: RoleResolver
    quota: QuotaService
    idempotency: Idempotency
    audit: AuditLog
    media: Any = None
    scheduler: Any = None
    # 持久化定时任务（kernel/jobs.py）。装配时注入；面板和 CLI 都通过它读写。
    jobs: Any = None
    # 出站闸门（kernel/outbound.py）。装配时注入，适配器发送统一走它。
    outbound: Any = None
    # 群维度配置闸门（kernel/channels.py）
    channel_gate: Any = None
    adapters: dict[str, Any] = field(default_factory=dict)
    # -- 能力提供者（P1）：能力名 → 实现；provider_owner 用于卸载时回收 ----- #
    providers: dict[str, Any] = field(default_factory=dict)
    provider_owner: dict[str, str] = field(default_factory=dict)
    # 服务契约：name -> ServiceSpec（提供方声明，消费方核对）
    service_specs: dict[str, Any] = field(default_factory=dict)
    def provide(self, capability: str, service: Any, owner: str, spec: Any = None) -> None:
        self.providers[capability] = service
        self.provider_owner[capability] = owner
        if spec is not None:
            self.service_specs[capability] = spec
    def withdraw(self, owner: str) -> list[str]:
        """撤掉某个插件提供的全部能力，返回被撤掉的能力名。"""
        removed = [cap for cap, who in self.provider_owner.items() if who == owner]
        for cap in removed:
            self.providers.pop(cap, None)
            self.provider_owner.pop(cap, None)
            self.service_specs.pop(cap, None)
        return removed
    def get_service(self, capability: str) -> Any:
        """给能力句柄用的惰性解析器 —— 调用时才查，所以支持热插拔。"""
        return self.providers.get(capability)
    # -- 运行期计数器（面板「概览」页用） --------------------------------- #
    started_at: float = field(default_factory=time.time)
    last_event_ts: float | None = None
    events_total: int = 0
    handled_total: int = 0
    def note_event(self, *, handled: bool) -> None:
        """每处理完一条事件调一次。只做自增，不落库 —— 面板要的是"现在"。"""
        self.events_total += 1
        if handled:
            self.handled_total += 1
        self.last_event_ts = time.time()
    def health(self) -> dict[str, Any]:
        """连接健康度汇总。`connected` 为 None 表示该平台没有可观测的连接态。"""
        platforms: dict[str, Any] = {}
        for name, adapter in self.adapters.items():
            info = adapter.describe()  # 已含 health
            platforms[name] = info
        any_connected = (
            any(p.get("health", {}).get("connected") is True for p in platforms.values()) or not platforms
        )
        return {
            "ok": any_connected and not self._degraded(),
            "uptime_seconds": max(0.0, time.time() - self.started_at),
            "started_at": self.started_at,
            "last_event_ts": self.last_event_ts,
            "events_total": self.events_total,
            "handled_total": self.handled_total,
            "platforms": platforms,
        }
    def _degraded(self) -> bool:
        """任一已启用平台明确掉线即视为降级（未知态不算）。"""
        for adapter in self.adapters.values():
            try:
                if adapter.health().get("connected") is False:
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False
    def advertise(self) -> dict[str, Any]:
        """给仪表盘/CLI 用的运行时快照。注意：不含任何凭据。"""
        return {
            "platforms": {name: a.describe() for name, a in self.adapters.items()},
            "middlewares": self.bus.middleware_names,
            "quota_enabled": self.config.quota.enabled,
            "vault_backend": self.vault.primary_backend,
            "uptime_seconds": max(0.0, time.time() - self.started_at),
            "started_at": self.started_at,
            "last_event_ts": self.last_event_ts,
            "events_total": self.events_total,
            "handled_total": self.handled_total,
            "outbound": self.outbound.stats() if self.outbound is not None else None,
        }
