"""治理层：角色、限速、配额、幂等、审计。

这一层是「群友刷表情包把额度烧光」的防线。
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field
from typing import Any

from haiqbot.config import PolicyCfg, QuotaCfg
from haiqbot.core.channel import ChannelId, Role
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)


class RoleResolver:
    """把平台上的 openid 映射成框架内的角色。"""

    def __init__(self, *, owners: list[str] | None = None, admins: list[str] | None = None) -> None:
        self.owners = set(owners or [])
        self.admins = set(admins or [])

    def resolve(self, actor_id: str, default: Role = Role.USER) -> Role:
        if str(actor_id) in self.owners:
            return Role.OWNER
        if str(actor_id) in self.admins:
            # 配置只能「提权」，不能把平台给的群主降成管理员
            return max(default, Role.ADMIN)
        return default

    @classmethod
    def from_policy(cls, policy: PolicyCfg) -> RoleResolver:
        return cls(owners=policy.owners, admins=policy.admins)


@dataclass
class TokenBucket:
    """按 key 的内存令牌桶。"""

    capacity: int = 60
    refill_per_sec: float = 1.0
    _tokens: dict[str, float] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)

    def allow(self, key: str, cost: float = 1.0, now: float | None = None) -> bool:
        if self.capacity <= 0:  # 0 = 不限速（配置里写 0 就是想关掉这一维）
            return True
        ts = now if now is not None else time.monotonic()
        tokens = self._tokens.get(key, float(self.capacity))
        last = self._last.get(key, ts)
        tokens = min(float(self.capacity), tokens + (ts - last) * self.refill_per_sec)
        self._last[key] = ts
        if tokens < cost:
            self._tokens[key] = tokens
            return False
        self._tokens[key] = tokens - cost
        return True


def _limit_with_override(overrides: dict[str, int] | None, key: str, fallback: int) -> int:
    """群级覆盖优先。**0 是合法值（不限制），不能当成"没配"回落全局值。**"""
    if overrides and key in overrides and overrides[key] is not None:
        return int(overrides[key])
    return int(fallback)


def today_key() -> str:
    return _dt.date.today().isoformat()


class QuotaService:
    """日配额 + 每分钟限速。

    四个旋钮（`per_user_per_day` / `per_channel_per_day` /
    `rate_per_user_per_min` / `rate_global_per_min`）统一语义：
    **> 0 生效；<= 0 表示这一维不限制**。注意方向别搞反——
    0 是"不限制"而不是"全禁"，后者会让所有人在第一条消息就被挡住。
    """

    def __init__(self, store: Any, cfg: QuotaCfg, resolver: RoleResolver | None = None) -> None:
        self.store = store
        self.cfg = cfg
        self.resolver = resolver or RoleResolver()
        self.user_bucket = TokenBucket(
            capacity=cfg.rate_per_user_per_min, refill_per_sec=cfg.rate_per_user_per_min / 60
        )
        self.global_bucket = TokenBucket(
            capacity=cfg.rate_global_per_min, refill_per_sec=cfg.rate_global_per_min / 60
        )

    async def check(
        self,
        *,
        actor_id: str,
        channel: ChannelId,
        overrides: dict[str, int] | None = None,
    ) -> tuple[bool, str]:
        """只做闸门判断，**不扣减**。真正的计费交给 `consume`。

        为什么要拆开：一次入站消息可能触发多个昂贵动作（比如 chat 里再调生图）。
        以前"按消息扣一份"，等于两个 API 调用只收一次钱，配额就成了摆设。
        """
        if not self.cfg.enabled:
            return True, ""
        if self.resolver.resolve(actor_id) >= Role.ADMIN:
            return True, ""

        if not self.global_bucket.allow("__global__"):
            return False, "全局限速中，请稍后"
        if not self.user_bucket.allow(str(actor_id)):
            return False, "手速太快了，慢一点"

        # 群维度可以覆盖每日配额（面板「群聊」页）；未配置（None）才回落全局值。
        # 注意：**0 是合法值 = 不限制**，不能用 `or` 判断，否则 0 会被当成"没配"。
        user_limit = _limit_with_override(overrides, "per_user_per_day", self.cfg.per_user_per_day)
        chan_limit = _limit_with_override(overrides, "per_channel_per_day", self.cfg.per_channel_per_day)

        day = today_key()
        # 0（或负数）= 不限制：这一维直接放行，连库都不查
        if user_limit > 0:
            used_user = await self.store.quota_get(f"user:{actor_id}", day)
            if used_user >= user_limit:
                return False, self.cfg.overflow_message
        if chan_limit > 0:
            used_chan = await self.store.quota_get(f"chan:{channel}", day)
            if used_chan >= chan_limit:
                return False, "本群今日额度已用完"
        return True, ""

    async def consume(self, *, actor_id: str, channel: ChannelId, cost: int = 1) -> None:
        """记一次消耗（不判断是否超限 —— 闸门在 `check`）。"""
        if not self.cfg.enabled or cost <= 0:
            return
        if self.resolver.resolve(actor_id) >= Role.ADMIN:
            return
        day = today_key()
        await self.store.quota_incr(f"user:{actor_id}", day, cost)
        await self.store.quota_incr(f"chan:{channel}", day, cost)

    async def check_and_consume(self, *, actor_id: str, channel: ChannelId) -> tuple[bool, str]:
        """老接口：判断 + 立刻扣一份。保留给测试与简单场景。"""
        ok, reason = await self.check(actor_id=actor_id, channel=channel)
        if ok:
            await self.consume(actor_id=actor_id, channel=channel)
        return ok, reason

    async def snapshot(self, actor_id: str, channel: ChannelId) -> dict[str, Any]:
        day = today_key()
        return {
            "day": day,
            "user_used": await self.store.quota_get(f"user:{actor_id}", day),
            "user_limit": self.cfg.per_user_per_day,
            "channel_used": await self.store.quota_get(f"chan:{channel}", day),
            "channel_limit": self.cfg.per_channel_per_day,
        }


class Idempotency:
    """msg_id 去重。官方文档明确：极端情况下同一条消息会重复推送。"""

    def __init__(self, store: Any, ttl: float = 600.0) -> None:
        self.store = store
        self.ttl = ttl

    async def first_time(self, msg_id: str) -> bool:
        if not msg_id:
            return True
        return await self.store.mark_seen(msg_id, self.ttl)


class AuditLog:
    def __init__(self, store: Any) -> None:
        self.store = store

    async def record(
        self,
        *,
        trace_id: str,
        plugin_id: str | None,
        action: str,
        channel: ChannelId | str | None = None,
        actor: str | None = None,
        detail: str | None = None,
    ) -> None:
        try:
            await self.store.audit(
                trace_id=trace_id,
                plugin_id=plugin_id,
                action=action,
                channel=str(channel) if channel is not None else None,
                actor=actor,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - 审计不能反过来影响主流程
            log.warning("audit_write_failed", action=action)
