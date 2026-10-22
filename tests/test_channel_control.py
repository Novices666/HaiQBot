"""频道开关的唯一写入口（S1）：三入口共用的语义与守卫。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from haiqbot.kernel import channel_control as cc
from haiqbot.kernel.channel_control import ChannelControlError


class FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.writes = 0

    async def channel_get(self, channel: str) -> dict[str, Any] | None:
        return self.rows.get(channel)

    async def channel_upsert(self, channel: str, **fields: Any) -> None:
        self.writes += 1
        self.rows.setdefault(channel, {"channel": channel}).update(fields)

    async def channel_all(self) -> list[dict[str, Any]]:
        return list(self.rows.values())


class FakeAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record(self, **kw: Any) -> None:
        self.calls.append(kw)


class FakeGate:
    def __init__(self) -> None:
        self.invalidated: list[str] = []

    def invalidate(self, channel: str | None = None) -> None:
        if channel:
            self.invalidated.append(channel)


class FakeRegistry:
    def discover(self) -> dict[str, str]:
        return {"painter": "entrypoint", "chat": "entrypoint", "echo": "entrypoint"}


class FakeRuntime:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.audit = FakeAudit()
        self.channel_gate = FakeGate()


def _rt() -> FakeRuntime:
    return FakeRuntime()


# --------------------------------------------------------------------------- #
# 语义
# --------------------------------------------------------------------------- #
async def test_default_is_nothing_enabled() -> None:
    rt = _rt()
    assert await cc.current(rt, "onebot:group:1") == []


async def test_enable_plugin_and_feature() -> None:
    rt = _rt()
    out = await cc.set_features(rt, FakeRegistry(), "onebot:group:1", on=["painter"], actor="t")
    assert out["enabled"] == ["painter"]
    assert out["changed"] == ["painter"]
    assert rt.channel_gate.invalidated == ["onebot:group:1"]
    assert rt.audit.calls[0]["action"] == "channel:features"

    out = await cc.set_features(rt, FakeRegistry(), "onebot:group:1", on=["chat:ask"], actor="t")
    assert out["enabled"] == ["chat:ask", "painter"]
    assert json.loads(rt.store.rows["onebot:group:1"]["plugins"]) == ["chat:ask", "painter"]


async def test_disable_only_target() -> None:
    rt = _rt()
    await cc.set_features(rt, FakeRegistry(), "c", on=["painter", "chat"], actor="t")
    out = await cc.set_features(rt, FakeRegistry(), "c", off=["chat"], actor="t")
    assert out["enabled"] == ["painter"]
    assert out["changed"] == ["chat"]


async def test_replace_wins() -> None:
    rt = _rt()
    await cc.set_features(rt, FakeRegistry(), "c", on=["painter", "chat"], actor="t")
    out = await cc.set_features(rt, FakeRegistry(), "c", replace=["echo"], on=["chat"], actor="t")
    assert out["enabled"] == ["echo"]


async def test_idempotent_write_does_not_touch_store() -> None:
    rt = _rt()
    await cc.set_features(rt, FakeRegistry(), "c", on=["painter"], actor="t")
    writes = rt.store.writes
    out = await cc.set_features(rt, FakeRegistry(), "c", on=["painter"], actor="t")
    assert out["changed"] == []
    assert rt.store.writes == writes, "没有变化时不该再写库"


async def test_unknown_plugin_rejected() -> None:
    rt = _rt()
    with pytest.raises(ChannelControlError) as exc:
        await cc.set_features(rt, FakeRegistry(), "c", on=["nope"], actor="t")
    assert "没有插件 nope" in exc.value.message
    assert rt.store.writes == 0


async def test_feature_syntax_normalised() -> None:
    assert cc.split_target("chat:ask") == ("chat", "ask")
    assert cc.split_target(" painter ") == ("painter", None)
    assert cc.normalise(["chat:ask", " chat:ask ", "painter", "painter"]) == ["chat:ask", "painter"]
    with pytest.raises(ChannelControlError):
        cc.split_target("chat:")
    with pytest.raises(ChannelControlError):
        cc.split_target("   ")


async def test_legacy_json_string_tolerated() -> None:
    rt = _rt()
    rt.store.rows["c"] = {"channel": "c", "plugins": '["painter"]'}
    assert await cc.current(rt, "c") == ["painter"]


async def test_disable_all_keeps_row_but_empties(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = _rt()
    await cc.set_features(rt, FakeRegistry(), "c", on=["painter"], actor="t")
    out = await cc.set_features(rt, FakeRegistry(), "c", replace=[], actor="t")
    assert out["enabled"] == []
    assert json.loads(rt.store.rows["c"]["plugins"]) == []
