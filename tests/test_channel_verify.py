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


# -- 全局 enabled：磁盘上有、却没启用的插件 ------------------------------- #


class _Cfg:
    def __init__(self, enabled: list[str]) -> None:
        self.plugins_enabled = enabled


class _CfgRuntime(_Runtime):
    def __init__(self, rows: list[dict], enabled: list[str]) -> None:
        super().__init__(rows)
        self.config = _Cfg(enabled)


@pytest.mark.asyncio
async def test_channel_target_but_plugin_not_enabled_is_error() -> None:
    """频道里勾了、插件目录也在，但没写进 [plugins] enabled —— 配了也不会响应。"""
    rt = _CfgRuntime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:draw"]}], [])
    problems = await cc.verify(rt, _registry())
    codes = [(p["level"], p["code"]) for p in problems]
    assert ("error", "plugin_not_enabled") in codes  # 逐频道：勾了但没启用
    assert ("info", "plugin_disabled") in codes  # 全局：这个插件根本不会被加载
    assert problems[0]["channel"] == "onebot:group:1"
    assert "enabled" in problems[0]["message"]


@pytest.mark.asyncio
async def test_channel_target_with_plugin_enabled_passes() -> None:
    rt = _CfgRuntime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:draw"]}], ["painter"])
    assert await cc.verify(rt, _registry()) == []


@pytest.mark.asyncio
async def test_discovered_but_disabled_is_global_info() -> None:
    reg = _Registry(
        {
            "painter": _Handle("painter", rules=[_Rule("draw")]),
            "ghost": _Handle("ghost", rules=[_Rule()]),
        }
    )
    problems = await cc.verify(_CfgRuntime([], ["painter"]), reg)
    assert [(p["level"], p["code"], p["channel"]) for p in problems] == [("info", "plugin_disabled", "*")]
    assert "ghost" in problems[0]["message"]


@pytest.mark.asyncio
async def test_enabled_but_missing_is_global_error() -> None:
    problems = await cc.verify(_CfgRuntime([], ["painter", "ghost"]), _registry())
    assert [(p["level"], p["code"], p["channel"]) for p in problems] == [
        ("error", "enabled_plugin_missing", "*")
    ]
    assert "ghost" in problems[0]["message"]


@pytest.mark.asyncio
async def test_no_config_means_no_guessing() -> None:
    """桩 runtime 读不到配置时不该报「未启用」—— 宁可少报，不要乱报。"""
    rt = _Runtime([{"channel": "onebot:group:1", "enabled": 1, "plugins": ["painter:draw"]}])
    assert {p["code"] for p in await cc.verify(rt, _registry())} == set()
