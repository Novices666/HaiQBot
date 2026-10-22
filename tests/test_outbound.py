"""出站闸门：同频道串行、最小间隔、全局令牌、频控退避、超时不重试。"""
from __future__ import annotations
import asyncio
import time
from pathlib import Path
import pytest
from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import OneBotCfg, OutboundCfg
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.errors import HaiqError
from haiqbot.core.segment import Segment
from haiqbot.kernel.outbound import OutboundBusy, OutboundLimiter, is_rate_limited

def _cfg(**kw) -> OutboundCfg:
    base = {
        "enabled": True,
        "min_interval_seconds": 0.0,
        "global_per_minute": 0,
        "max_wait_seconds": 5.0,
        "retry_max": 2,
        "retry_backoff_seconds": 0.0,
        "retry_max_backoff_seconds": 0.0,
    }
    base.update(kw)
    return OutboundCfg(**base)

# -- 判据 ------------------------------------------------------------------- #

def test_rate_limit_judge() -> None:
    assert is_rate_limited(HaiqError("OneBot 发送失败：1200"))
    assert is_rate_limited(HaiqError("发送过于频繁，请稍后再试"))
    assert is_rate_limited(HaiqError("rate limit exceeded"))
    # 超时/参数错误不该被当成频控 —— 重试只会把同一条消息发两遍
    assert not is_rate_limited(HaiqError("OneBot 调用超时：send_group_msg（15s）"))
    assert not is_rate_limited(HaiqError("消息太长"))

# -- 间隔与串行 ------------------------------------------------------------- #

async def test_min_interval_spaces_same_channel() -> None:
    gate = OutboundLimiter(_cfg(min_interval_seconds=0.15))
    stamps: list[float] = []
    async def send() -> None:
        stamps.append(time.monotonic())
    await gate.send("onebot:group:1", send)
    await gate.send("onebot:group:1", send)
    assert stamps[1] - stamps[0] >= 0.12, "同频道两条之间必须留间隔"

async def test_different_channels_are_not_blocked_by_interval() -> None:
    gate = OutboundLimiter(_cfg(min_interval_seconds=5.0))
    await gate.send("onebot:group:1", lambda: asyncio.sleep(0))
    t0 = time.monotonic()
    await gate.send("onebot:group:2", lambda: asyncio.sleep(0))
    assert time.monotonic() - t0 < 1.0, "间隔只约束同一个频道"

async def test_same_channel_is_serialised() -> None:
    """两条并发发送不能同时在途，否则间隔白算、msg_seq 也会打架。"""
    gate = OutboundLimiter(_cfg(min_interval_seconds=0.0))
    running = 0
    peak = 0
    async def slow() -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
    await asyncio.gather(
        gate.send("onebot:group:9", slow),
        gate.send("onebot:group:9", slow),
        gate.send("onebot:group:9", slow),
    )
    assert peak == 1, "同频道同刻只允许一条在途"

# -- 全局限速 --------------------------------------------------------------- #

async def test_global_rate_limits_across_channels() -> None:
    # 每分钟 60 条 → 每秒 1 条；桶容量 3，所以前 3 条秒过，第 4 条要等
    gate = OutboundLimiter(_cfg(global_per_minute=60))
    t0 = time.monotonic()
    for i in range(4):
        await gate.send(f"onebot:group:{i}", lambda: asyncio.sleep(0))
    assert time.monotonic() - t0 >= 0.9, "全局令牌桶要压住跨频道的发送速率"

async def test_zero_global_rate_means_unlimited() -> None:
    gate = OutboundLimiter(_cfg(global_per_minute=0))
    t0 = time.monotonic()
    for i in range(20):
        await gate.send(f"onebot:group:{i}", lambda: asyncio.sleep(0))
    assert time.monotonic() - t0 < 1.0

# -- 重试 ------------------------------------------------------------------- #

async def test_retries_rate_limit_then_succeeds() -> None:
    gate = OutboundLimiter(_cfg(retry_max=2))
    calls: list[int] = []
    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise HaiqError("发送过于频繁")
        return "ok"
    assert await gate.send("onebot:group:1", flaky) == "ok"
    assert len(calls) == 3
    assert gate.stats()["retried_total"] == 2

async def test_gives_up_after_retry_budget() -> None:
    gate = OutboundLimiter(_cfg(retry_max=1))
    calls: list[int] = []
    async def always() -> None:
        calls.append(1)
        raise HaiqError("too frequent")
    with pytest.raises(HaiqError):
        await gate.send("onebot:group:1", always)
    assert len(calls) == 2, "retry_max=1 → 一共两次尝试"

async def test_timeout_is_not_retried() -> None:
    """超时可能已经送达；重试 = 群里出现两条一样的消息。"""
    gate = OutboundLimiter(_cfg(retry_max=5))
    calls: list[int] = []
    async def timeout() -> None:
        calls.append(1)
        raise HaiqError("OneBot 调用超时：send_group_msg（120s）")
    with pytest.raises(HaiqError):
        await gate.send("onebot:group:1", timeout)
    assert len(calls) == 1

async def test_custom_judge_can_widen_retry() -> None:
    gate = OutboundLimiter(_cfg(retry_max=1))
    calls: list[int] = []
    async def flaky() -> str:
        calls.append(1)
        if len(calls) < 2:
            raise HaiqError("连接被重置")
        return "ok"
    assert await gate.send("onebot:group:1", flaky, retryable=lambda _e: True) == "ok"
    assert len(calls) == 2

# -- 排队上限 --------------------------------------------------------------- #

async def test_busy_channel_drops_instead_of_queueing_forever() -> None:
    gate = OutboundLimiter(_cfg(max_wait_seconds=0.1))
    async def hold() -> None:
        await asyncio.sleep(1.0)
    task = asyncio.create_task(gate.send("onebot:group:1", hold))
    await asyncio.sleep(0.02)
    with pytest.raises(OutboundBusy):
        await gate.send("onebot:group:1", lambda: asyncio.sleep(0))
    assert gate.stats()["dropped_total"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

# -- 关闭开关 --------------------------------------------------------------- #

async def test_disabled_gate_is_transparent() -> None:
    gate = OutboundLimiter(_cfg(enabled=False, min_interval_seconds=10.0))
    t0 = time.monotonic()
    for _ in range(3):
        await gate.send("onebot:group:1", lambda: asyncio.sleep(0))
    assert time.monotonic() - t0 < 1.0

# -- 适配器接线 ------------------------------------------------------------- #

async def test_onebot_send_goes_through_gate(tmp_path: Path) -> None:
    """适配器必须真的把发送交出去 —— 否则闸门等于没装。"""
    gate = OutboundLimiter(_cfg(min_interval_seconds=0.1))
    adapter = OneBotAdapter(OneBotCfg(), token_provider=lambda: None)
    adapter.outbound = gate
    seen: list[float] = []
    async def fake_call(action: str, params: dict, **_kw: object) -> dict:
        seen.append(time.monotonic())
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
    adapter.call = fake_call  # type: ignore[method-assign]
    channel = ChannelId("onebot", ChannelKind.GROUP, "123")
    await adapter.send_message(channel, [Segment.text("a")])
    await adapter.send_message(channel, [Segment.text("b")])
    assert seen[1] - seen[0] >= 0.09
    assert gate.stats()["sent_total"] == 2

async def test_onebot_send_retries_when_rate_limited(tmp_path: Path) -> None:
    gate = OutboundLimiter(_cfg())
    adapter = OneBotAdapter(OneBotCfg(), token_provider=lambda: None)
    adapter.outbound = gate
    calls: list[int] = []
    async def fake_call(action: str, params: dict, **_kw: object) -> dict:
        calls.append(1)
        if len(calls) == 1:
            return {"status": "failed", "retcode": 1200}
        return {"status": "ok", "retcode": 0, "data": {"message_id": 2}}
    adapter.call = fake_call  # type: ignore[method-assign]
    mid = await adapter.send_message(ChannelId("onebot", ChannelKind.GROUP, "123"), [Segment.text("x")])
    assert mid == "2"
    assert len(calls) == 2
