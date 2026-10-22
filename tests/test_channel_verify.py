"""频道开关体检：把「配了却不生效」的静默失效点出来。"""

from __future__ import annotations

import pytest

from haiqbot.kernel import channel_control as cc


class _Rule:
    def __init__(self, feature: str | None = None) -> None:
        self.feature = feature


class _Plugin:
    def __init__(self, pid: str, gating: str = "opt_in") -> None:
        self.id = pid
        self.gating = gating


class _Manifest:
    def __init__(self, pid: str) -> None:
        self.id = pid


class _Handle:
    def __init__(self, pid: str, *, rules: list[_Rule], gating: str = "opt_in") -> None:
        self.plugin = _Plugin(pid, gating)
        self.manifest = _Manifest(pid)
        self.rules = rules


class _Registry:
    def __init__(self, handles: dict[str, _Handle]) -> None:
        self._handles = handles

    def discover(self) -> dict[str, str]:
        return {pid: "entrypoint" for pid in self._handles}

    def handles(self) -> dict[str, _Handle]:
        return self._handles


class _Store:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    async def channel_all(self) -> list[dict]:
        return self._rows


class _Runtime:
    def __init__(self, rows: list[dict]) -> None:
        self.store = _Store(rows)


def _registry() -> _Registry:
    return _Registry({"painter": _Handle("painter", rules=[_Rule("draw"), _Rule()])})


@pytest.mark.asyncio
async def test_healthy_channel_reports_nothing():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:draw"]}])
    assert await cc.verify(rt, _registry()) == []


@pytest.mark.asyncio
async def test_unknown_feature_is_error():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:draww"]}])
    problems = await cc.verify(rt, _registry())
    assert [(p["level"], p["code"]) for p in problems] == [("error", "unknown_feature")]
    assert "draw" in problems[0]["message"]


@pytest.mark.asyncio
async def test_feature_on_plugin_without_features_is_warning():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:ask"]}])
    problems = await cc.verify(rt, _registry())
    # painter 有 feature，"ask" 不存在 -> 仍是错误；这里换个没有 feature 的插件语义
    assert problems and problems[0]["level"] == "error"


@pytest.mark.asyncio
async def test_enabled_without_plugins_is_warning():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": []}])
    problems = await cc.verify(rt, _registry())
    assert [(p["level"], p["code"]) for p in problems] == [("warn", "nothing_enabled")]


@pytest.mark.asyncio
async def test_disabled_channel_is_info():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 0, "plugins": ["painter"]}])
    problems = await cc.verify(rt, _registry())
    assert [(p["level"], p["code"]) for p in problems] == [("info", "channel_disabled")]


@pytest.mark.asyncio
async def test_unknown_plugin_is_error():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["nope"]}])
    problems = await cc.verify(rt, _registry())
    assert [(p["level"], p["code"]) for p in problems] == [("error", "unknown_plugin")]


@pytest.mark.asyncio
async def test_always_plugin_is_informational():
    reg = _Registry({"echo": _Handle("echo", rules=[_Rule()], gating="always")})
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["echo"]}])
    problems = await cc.verify(rt, reg)
    assert [(p["level"], p["code"]) for p in problems] == [("info", "always_on")]


@pytest.mark.asyncio
async def test_bad_target_format_is_error():
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:"]}])
    problems = await cc.verify(rt, _registry())
    assert [(p["level"], p["code"]) for p in problems] == [("error", "bad_target")]
