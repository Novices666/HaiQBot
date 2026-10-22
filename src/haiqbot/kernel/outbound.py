"""出站闸门：同频道串行 + 最小间隔 + 全局速率 + 频控退避重试。

为什么必须有这一层
------------------
QQ 对同一个账号在短时间内的发送频率有阈值。超了的代价不是"消息发慢一点"，
而是**消息直接发不出去**，严重时整号进入风控。而 haiqbot 的两枪回复
（`ack()` → 长任务 → 结果）天然会在几秒内连发两条，生图排队时更容易成簇。

所以出站一律走这里，而不是在业务代码里各自 `sleep`：

- **同频道串行**：一个群同时只允许一条在途，避免两条消息抢同一个 msg_seq。
- **同频道最小间隔**：两条之间至少隔 `min_interval_seconds`。
- **全局速率**：跨所有频道的令牌桶（`global_per_minute`），对应"这个号总共别太快"。
- **排队上限**：等不到槽位就放弃并抛 `OutboundBusy` —— 宁可丢一条，也不让队列
  无限堆积成雪崩（消息越排越旧，最后发出去全是过期回复）。
- **退避重试**：只对**明确未被投递**的失败重试（见 `retryable` 判据）。

重试语义（重要）
----------------
发送不是幂等的。重试一个"可能已经投递成功"的请求 = 群里出现两条一样的消息。
因此默认判据 `is_rate_limited` 只认平台明确回报的**频控类**错误；超时 **不重试**
（超时可能已经送达），由调用方决定是否补发。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from haiqbot.core.errors import HaiqError, MediaSendError
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)

T = TypeVar("T")

# 频控类错误的关键特征。NapCat / OneBot 实现各写各的，只能宽松匹配：
# - retcode 1200/1201/1202：OneBot 实现里常见的"发送过于频繁"
# - 文本里带"频繁 / 太快 / 风控 / rate limit / too fast"
_RATE_HINTS = ("频繁", "太快", "发送过快", "风控", "rate limit", "too fast", "too frequent", "frequency")
_RATE_RETCODES = {1200, 1201, 1202}


class OutboundBusy(HaiqError):
    """排队超时：出站通道太挤，本条放弃。"""


def is_rate_limited(exc: BaseException) -> bool:
    """失败是否属于"被平台限速、明确没发出去"。"""
    text = str(exc).lower()
    if any(h in text for h in _RATE_HINTS):
        return True
    for code in _RATE_RETCODES:
        if str(code) in text:
            return True
    return False


class OutboundLimiter:
    """出站闸门。线程/任务安全（单事件循环内）。"""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_sent: dict[str, float] = {}
        # 全局令牌桶
        self._tokens = self._capacity()
        self._tokens_at = time.monotonic()
        self._bucket_lock = asyncio.Lock()
        # 观测
        self.sent_total = 0
        self.retried_total = 0
        self.dropped_total = 0

    # -- 槽位 ------------------------------------------------------------- #
    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _capacity(self) -> float:
        """桶容量 = 允许的小突发量。既不做"一分钟 N 条就只能一秒一条"的死板平滑，
        也不允许一瞬间把一分钟的额度全打出去（那正好撞风控）。"""
        rate = max(float(getattr(self.cfg, "global_per_minute", 0)), 0.0)
        if rate <= 0:
            return 1.0
        return max(1.0, min(rate, 3.0))

    def _refill(self) -> None:
        rate = max(float(getattr(self.cfg, "global_per_minute", 0)), 0.0)
        now = time.monotonic()
        if rate <= 0:
            self._tokens = 1.0  # 关掉全局限速
            self._tokens_at = now
            return
        self._tokens = min(self._capacity(), self._tokens + (now - self._tokens_at) * (rate / 60.0))
        self._tokens_at = now

    async def _take_token(self) -> None:
        """拿一个全局令牌；拿不到就等到有为止。"""
        while True:
            async with self._bucket_lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                rate = max(float(getattr(self.cfg, "global_per_minute", 0)), 1.0)
                wait = (1.0 - self._tokens) * (60.0 / rate)
            await asyncio.sleep(wait)

    async def _pace(self, key: str) -> None:
        """同频道最小间隔 + 全局令牌。调用方必须已持有该频道的锁。"""
        interval = max(float(getattr(self.cfg, "min_interval_seconds", 0.0)), 0.0)
        if interval > 0:
            last = self._last_sent.get(key)
            if last is not None:
                gap = interval - (time.monotonic() - last)
                if gap > 0:
                    log.debug("outbound_paced", channel=key, sleep_s=round(gap, 2))
                    await asyncio.sleep(gap)
        await self._take_token()
        self._last_sent[key] = time.monotonic()

    # -- 主入口 ----------------------------------------------------------- #
    async def send(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        retryable: Callable[[BaseException], bool] | None = None,
        plugin_id: str | None = None,
    ) -> T:
        """排队 → 执行 → 频控退避重试。`key` 形如 `onebot:group:123456`。

        整个发送过程（含重试）都持有该频道的锁 —— 同频道必须真的串行，
        否则"检查间隔 → 发送"之间会插进来第二条，间隔就白算了。
        """
        if not getattr(self.cfg, "enabled", True):
            return await fn()

        judge = retryable or is_rate_limited
        attempts = max(int(getattr(self.cfg, "retry_max", 0)), 0) + 1
        max_wait = float(getattr(self.cfg, "max_wait_seconds", 30.0))
        base = float(getattr(self.cfg, "retry_backoff_seconds", 2.0))
        cap = float(getattr(self.cfg, "retry_max_backoff_seconds", 20.0))

        lock = self._lock_for(key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=max_wait if max_wait > 0 else None)
        except TimeoutError as exc:
            self.dropped_total += 1
            log.warning("outbound_busy", channel=key, waited_s=max_wait, plugin=plugin_id)
            raise OutboundBusy(f"出站排队超过 {max_wait:.0f}s，本次放弃：{key}") from exc

        try:
            await self._pace(key)
            last: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    result = await fn()
                    self.sent_total += 1
                    return result
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 - 判据决定去留
                    last = exc
                    # 媒体被平台拒绝：重试只会加重风控 + 可能重复，直接放弃
                    if isinstance(exc, MediaSendError):
                        log.warning("outbound_media_rejected", channel=key, plugin=plugin_id)
                        raise
                    if attempt >= attempts or not judge(exc):
                        raise
                    self.retried_total += 1
                    delay = min(cap, base * (2 ** (attempt - 1)))
                    delay += random.uniform(0, delay * 0.3) if delay else 0.0  # jitter，别同步撞车
                    log.warning(
                        "outbound_retry",
                        channel=key,
                        attempt=attempt,
                        max_attempts=attempts,
                        sleep_s=round(delay, 2),
                        error=type(exc).__name__,
                        plugin=plugin_id,
                    )
                    await asyncio.sleep(delay)
                    # 重试也要重新排队拿槽位，否则连发重试本身又是一次风控
                    await self._pace(key)
            assert last is not None  # 循环至少跑一次，失败才落到这里
            raise last
        finally:
            lock.release()

    # -- 观测 ------------------------------------------------------------- #
    def stats(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(self.cfg, "enabled", True)),
            "sent_total": self.sent_total,
            "retried_total": self.retried_total,
            "dropped_total": self.dropped_total,
            "channels_tracked": len(self._last_sent),
            "tokens": round(self._tokens, 2),
        }
