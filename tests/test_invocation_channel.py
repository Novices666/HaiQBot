"""群维度闸门（channel_config）。

调用流水已不由内核写（Q2=A）：内核不再认识生图/对话，也就不该替它们记账。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from haiqbot.adapters.mock import MockAdapter
from haiqbot.app import App
from haiqbot.config import HaiqConfig, MediaCfg, PolicyCfg, QuotaCfg, WebuiCfg
from haiqbot.core.channel import Actor, ChannelId, ChannelKind
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.segment import Segment
from haiqbot.kernel.channels import ChannelGate


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _event(text: str, msg_id: str, *, channel: str = "9") -> MessageEvent:
    return MessageEvent(
        kind=EventKind.MESSAGE,
        channel=ChannelId("mock", ChannelKind.GROUP, channel),
        actor=Actor(id="u-inv"),
        segments=[Segment.text(text)],
        msg_id=msg_id,
        at_bot=True,
        bot_id="mock-bot",
    )


def _cfg(tmp_path: Path, **kw) -> HaiqConfig:
    base = {
        "data_dir": tmp_path / "data",
        "db_path": tmp_path / "data" / "haiq.db",
        "secrets_dir": tmp_path / "secrets",
        "log_level": "CRITICAL",
        "plugins_enabled": ["echo", "painter", "chat"],
        "quota": QuotaCfg(
            enabled=True, per_user_per_day=50, per_channel_per_day=50, rate_per_user_per_min=100
        ),
        "policy": PolicyCfg(handle_timeout_seconds=5.0),
        "media": MediaCfg(dir=tmp_path / "data" / "media"),
        "webui": WebuiCfg(enabled=False),
    }
    base.update(kw)
    return HaiqConfig(**base)


async def _boot(cfg: HaiqConfig) -> tuple[App, MockAdapter]:
    app = App(cfg)
    await app.setup(demo=True)
    await app.start_plugins()
    await app.start_adapters()
    return app, app.adapters["mock"]  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 群维度闸门
# --------------------------------------------------------------------------- #
@pytest.fixture
async def raw_store(tmp_path: Path):
    from haiqbot.kernel.store import Store

    store = await Store(tmp_path / "cfg.db").open()
    yield store
    await store.close()


async def test_channel_gate_defaults_to_closed(raw_store: object) -> None:
    """新契约：没有配置 = **全关**（不是"不限"）。"""
    gate = ChannelGate(raw_store)
    assert await gate.row("onebot:group:1") is None
    assert await gate.allowed_plugins("onebot:group:1") == set()
    assert await gate.quota_overrides("onebot:group:1") is None


async def test_channel_gate_reads_row_and_caches(raw_store: object) -> None:
    store = raw_store
    await store.channel_upsert(
        "onebot:group:1", enabled=0, quota_user=3, quota_chan=None, plugins=["echo"], note="测试"
    )
    gate = ChannelGate(store)
    row = await gate.row("onebot:group:1")
    assert row is not None and row["enabled"] == 0
    assert await gate.allowed_plugins("onebot:group:1") == {"echo"}
    assert await gate.quota_overrides("onebot:group:1") == {"per_user_per_day": 3}


async def test_disabled_channel_is_silent(tmp_path: Path) -> None:
    app, mock = await _boot(_cfg(tmp_path))
    try:
        await app.store.channel_upsert("mock:group:9", enabled=0)
        app.runtime.channel_gate.invalidate()
        before = len(mock.sent)
        await mock.inject(_event("/echo 你好", "s1"))
        await asyncio.sleep(0.1)
        assert len(mock.sent) == before, "整群静默后不该有任何回话"
    finally:
        await app.shutdown()


async def test_plugin_allowlist_limits_handlers(tmp_path: Path) -> None:
    app, mock = await _boot(_cfg(tmp_path))
    try:
        # 只放行 echo：画图/对话都不该响应
        await app.store.channel_upsert("mock:group:9", enabled=1, plugins=["echo"])
        app.runtime.channel_gate.invalidate()
        texts_before = len(mock.texts())
        await mock.inject(_event("/画图 猫", "a1"))
        await asyncio.sleep(0.15)
        assert len(mock.texts()) == texts_before, "未放行的插件不得处理该群消息"

        await mock.inject(_event("/echo 你好", "a2"))
        await asyncio.sleep(0.05)
        assert any("你好" in t for t in mock.texts())
    finally:
        await app.shutdown()


async def test_channel_quota_override_is_applied(tmp_path: Path) -> None:
    app, mock = await _boot(_cfg(tmp_path))
    try:
        # 本群每人只给 0 次：第一次就被拦
        await app.store.channel_upsert(
            "mock:group:9", enabled=1, quota_user=1, quota_chan=None, plugins=list(app.registry.handles())
        )
        app.runtime.channel_gate.invalidate()
        await app.store.quota_incr("user:u-inv", _today(), 1)
        await mock.inject(_event("/echo hi", "q1"))
        await asyncio.sleep(0.05)
        assert any("配额" in t for t in mock.texts()), "超过群内配额要给出提示"
    finally:
        await app.shutdown()


def _today() -> str:
    from haiqbot.kernel.governance import today_key

    return today_key()


async def test_runtime_exposes_channel_gate(tmp_path: Path) -> None:
    app, _ = await _boot(_cfg(tmp_path))
    try:
        assert isinstance(app.runtime.channel_gate, ChannelGate)
    finally:
        await app.shutdown()


async def test_gate_tolerates_broken_store() -> None:
    class Bad:
        async def channel_get(self, _channel: str) -> dict:
            raise RuntimeError("db down")

    gate = ChannelGate(Bad())
    assert await gate.row("onebot:group:1") is None
