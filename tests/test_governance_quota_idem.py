"""治理：令牌桶、日配额、幂等、角色。"""

from __future__ import annotations

from haiqbot.config import QuotaCfg
from haiqbot.core.channel import ChannelId, ChannelKind, Role
from haiqbot.kernel.governance import (
    Idempotency,
    QuotaService,
    RoleResolver,
    TokenBucket,
    today_key,
)
from haiqbot.kernel.store import Store


def test_token_bucket_refills() -> None:
    bucket = TokenBucket(capacity=2, refill_per_sec=1.0)
    assert bucket.allow("k", now=0.0) is True
    assert bucket.allow("k", now=0.0) is True
    assert bucket.allow("k", now=0.0) is False
    assert bucket.allow("k", now=1.0) is True  # 补了 1 个令牌


def test_role_resolver() -> None:
    resolver = RoleResolver(owners=["owner1"], admins=["admin1"])
    assert resolver.resolve("owner1") is Role.OWNER
    assert resolver.resolve("admin1") is Role.ADMIN
    assert resolver.resolve("someone") is Role.USER


async def test_daily_quota_blocks_and_admin_bypasses(store: Store) -> None:
    cfg = QuotaCfg(enabled=True, per_user_per_day=2, per_channel_per_day=100, rate_per_user_per_min=100)
    resolver = RoleResolver(admins=["admin1"])
    quota = QuotaService(store, cfg, resolver)
    channel = ChannelId("mock", ChannelKind.GROUP, "1")

    for _ in range(2):
        ok, _reason = await quota.check_and_consume(actor_id="u1", channel=channel)
        assert ok is True

    ok, reason = await quota.check_and_consume(actor_id="u1", channel=channel)
    assert ok is False
    assert reason == cfg.overflow_message

    # 管理员不受日配额限制
    for _ in range(5):
        ok, _ = await quota.check_and_consume(actor_id="admin1", channel=channel)
        assert ok is True


async def test_channel_quota_blocks(store: Store) -> None:
    cfg = QuotaCfg(enabled=True, per_user_per_day=100, per_channel_per_day=2, rate_per_user_per_min=100)
    quota = QuotaService(store, cfg)
    channel = ChannelId("mock", ChannelKind.GROUP, "9")
    assert (await quota.check_and_consume(actor_id="u1", channel=channel))[0] is True
    assert (await quota.check_and_consume(actor_id="u2", channel=channel))[0] is True
    ok, reason = await quota.check_and_consume(actor_id="u3", channel=channel)
    assert ok is False
    assert "本群" in reason


async def test_zero_user_quota_means_unlimited(store: Store) -> None:
    """0 = 不限制，**不是全禁**。

    历史坑：`used >= limit` 在 limit=0 时恒真，会把所有人挡在第一条消息。
    """
    cfg = QuotaCfg(enabled=True, per_user_per_day=0, per_channel_per_day=100, rate_per_user_per_min=1000)
    quota = QuotaService(store, cfg)
    channel = ChannelId("mock", ChannelKind.GROUP, "1")
    for _ in range(50):
        ok, reason = await quota.check_and_consume(actor_id="u1", channel=channel)
        assert ok is True, reason


async def test_zero_channel_quota_means_unlimited(store: Store) -> None:
    cfg = QuotaCfg(enabled=True, per_user_per_day=100, per_channel_per_day=0, rate_per_user_per_min=1000)
    quota = QuotaService(store, cfg)
    channel = ChannelId("mock", ChannelKind.GROUP, "9")
    for i in range(30):
        ok, reason = await quota.check_and_consume(actor_id=f"u{i}", channel=channel)
        assert ok is True, reason


def test_zero_rate_means_no_throttle() -> None:
    """限流旋钮写 0 = 不限速；capacity=0 的令牌桶必须放行而不是全禁。"""
    bucket = TokenBucket(capacity=0, refill_per_sec=0)
    for _ in range(20):
        assert bucket.allow("k", now=0.0) is True


async def test_channel_override_zero_wins_over_global(store: Store) -> None:
    """群级覆盖写 0 必须生效（= 本群不限制），不能回落全局值 2。"""
    cfg = QuotaCfg(enabled=True, per_user_per_day=2, per_channel_per_day=100, rate_per_user_per_min=1000)
    quota = QuotaService(store, cfg)
    channel = ChannelId("mock", ChannelKind.GROUP, "7")
    overrides = {"per_user_per_day": 0}
    for _ in range(10):
        ok, reason = await quota.check(actor_id="u1", channel=channel, overrides=overrides)
        assert ok is True, reason
        await quota.consume(actor_id="u1", channel=channel)

    # 对照：不给覆盖时，全局值 2 仍然生效
    ok, reason = await quota.check(actor_id="u1", channel=channel)
    assert ok is False
    assert reason == cfg.overflow_message


async def test_quota_disabled_always_allows(store: Store) -> None:
    quota = QuotaService(store, QuotaCfg(enabled=False, per_user_per_day=0))
    channel = ChannelId("mock", ChannelKind.GROUP, "1")
    for _ in range(10):
        assert (await quota.check_and_consume(actor_id="u1", channel=channel))[0] is True


async def test_idempotency_first_time_only(store: Store) -> None:
    idem = Idempotency(store)
    assert await idem.first_time("m1") is True
    assert await idem.first_time("m1") is False
    assert await idem.first_time("m2") is True
    assert await idem.first_time("") is True  # 空 id 不拦


async def test_quota_snapshot(store: Store) -> None:
    cfg = QuotaCfg(enabled=True, per_user_per_day=10, per_channel_per_day=20)
    quota = QuotaService(store, cfg)
    channel = ChannelId("mock", ChannelKind.GROUP, "1")
    await quota.check_and_consume(actor_id="u1", channel=channel)
    snap = await quota.snapshot("u1", channel)
    assert snap["day"] == today_key()
    assert snap["user_used"] == 1
    assert snap["user_limit"] == 10
