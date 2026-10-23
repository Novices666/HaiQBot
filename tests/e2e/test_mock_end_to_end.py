"""端到端（零成本）：mock 适配器 + mock provider，跑完整链路。"""

from __future__ import annotations

from pathlib import Path

import pytest

from haiqbot.app import App
from haiqbot.config import HaiqConfig, MediaCfg, PolicyCfg, QuotaCfg, WebuiCfg


@pytest.fixture
def full_cfg(tmp_path: Path) -> HaiqConfig:
    return HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        log_level="CRITICAL",
        plugins_enabled=["echo", "help", "assistant"],
        quota=QuotaCfg(enabled=True, per_user_per_day=10, per_channel_per_day=50),
        policy=PolicyCfg(handle_timeout_seconds=5.0),
        media=MediaCfg(dir=tmp_path / "data" / "media", ttl_seconds=3600),
        webui=WebuiCfg(enabled=False),
    )


async def test_demo_flow(full_cfg: HaiqConfig) -> None:
    app = App(full_cfg)
    result = await app.demo()

    texts = [m["text"] for m in result["sent"]]
    assert any("可用命令" in t for t in texts), "/help 应输出命令汇总"

    images = [seg for m in result["sent"] for seg in m["segments"] if seg["kind"] == "image"]
    assert images, "应当发出至少一条图片消息"
    url = images[0]["data"]["url"]
    assert "/i/" in url
    assert "?" not in url, "对外图片 URL 不能带查询串"


async def test_runtime_sees_adapters(full_cfg: HaiqConfig) -> None:
    """仪表盘 /api/status 依赖 runtime.adapters，必须与 App.adapters 同源。"""
    app = App(full_cfg)
    await app.setup(demo=True)
    await app.start_plugins()
    await app.start_adapters()
    # 默认全关语义：测试里要显式开启 mock 频道
    for _ch in ("mock:group:9", "mock:group:10001", "mock:group:1"):
        await app.store.channel_upsert(_ch, enabled=1, plugins=list(app.registry.handles()))
    app.runtime.channel_gate.invalidate()

    assert app.runtime is not None
    assert "mock" in app.runtime.adapters
    advertised = app.runtime.advertise()
    assert "mock" in advertised["platforms"]
    assert advertised["platforms"]["mock"]["capabilities"]["image"] is True

    await app.shutdown()


async def test_pipeline_dedupe_and_quota(full_cfg: HaiqConfig) -> None:
    """同一条 msg_id 只处理一次；超配额被拦住。"""
    from haiqbot.core.channel import Actor, ChannelId, ChannelKind
    from haiqbot.core.event import EventKind, MessageEvent
    from haiqbot.core.segment import Segment

    app = App(full_cfg)
    await app.setup(demo=True)
    await app.start_plugins()
    await app.start_adapters()
    for _ch in ("mock:group:42", "mock:group:9", "mock:group:10001"):
        await app.store.channel_upsert(_ch, enabled=1, plugins=list(app.registry.handles()))
    app.runtime.channel_gate.invalidate()
    mock = app.adapters["mock"]

    channel = ChannelId("mock", ChannelKind.GROUP, "42")
    event = MessageEvent(
        kind=EventKind.MESSAGE,
        channel=channel,
        actor=Actor(id="u1"),
        segments=[Segment.text("/echo 一")],
        msg_id="same-id",
        at_bot=True,
    )
    await mock.inject(event)
    sent_after_first = len(mock.sent)

    # 同 msg_id 再投一次 → 被 dedupe 拦掉
    await mock.inject(
        MessageEvent(
            kind=EventKind.MESSAGE,
            channel=channel,
            actor=Actor(id="u1"),
            segments=[Segment.text("/echo 一")],
            msg_id="same-id",
            at_bot=True,
        )
    )
    assert len(mock.sent) == sent_after_first

    # 逐条不同 msg_id 打满日配额
    full_cfg.quota.per_user_per_day = 2
    for i in range(5):
        await mock.inject(
            MessageEvent(
                kind=EventKind.MESSAGE,
                channel=channel,
                actor=Actor(id="u2"),
                segments=[Segment.text(f"/echo {i}")],
                msg_id=f"q-{i}",
                at_bot=True,
            )
        )
    texts = [m["text"] for m in mock.sent]
    assert any("配额" in t or "额度" in t for t in texts), "超配额应有提示"

    await app.shutdown()


async def test_unknown_command_is_silent(full_cfg: HaiqConfig) -> None:
    from haiqbot.core.channel import Actor, ChannelId, ChannelKind
    from haiqbot.core.event import EventKind, MessageEvent
    from haiqbot.core.segment import Segment

    app = App(full_cfg)
    await app.setup(demo=True)
    await app.start_plugins()
    await app.start_adapters()
    mock = app.adapters["mock"]

    before = len(mock.sent)
    await mock.inject(
        MessageEvent(
            kind=EventKind.MESSAGE,
            channel=ChannelId("mock", ChannelKind.GROUP, "7"),
            actor=Actor(id="u9"),
            segments=[Segment.text("/不存在的命令")],
            msg_id="nope-1",
            at_bot=True,
        )
    )
    assert len(mock.sent) == before, "没有插件认领时应保持安静"
    await app.shutdown()
