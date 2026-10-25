"""面板改连接参数 → 就地重连接入端（而不是让用户重启进程）。"""

from __future__ import annotations

from typing import Any

import pytest

from haiqbot.webui.api.config_api import _reconnect_if_needed


class _Runtime:
    def __init__(self, platforms: list[str] | None = None, boom: Exception | None = None) -> None:
        self.called = 0
        self._platforms = platforms if platforms is not None else ["onebot"]
        self._boom = boom

    async def restart_adapters(self) -> list[str]:
        self.called += 1
        if self._boom is not None:
            raise self._boom
        return self._platforms


def _result(*paths: str, warnings: list[str] | None = None) -> dict[str, Any]:
    return {
        "changed": [{"path": p, "apply": "reconnect", "apply_label": "需重连"} for p in paths],
        "warnings": list(warnings or []),
    }


@pytest.mark.asyncio
async def test_onebot_change_triggers_reconnect() -> None:
    runtime = _Runtime()
    result = _result("onebot.url", warnings=["以下改动需要额外操作才生效：需重连"])

    platforms = await _reconnect_if_needed(runtime, result)

    assert platforms == ["onebot"]
    assert runtime.called == 1
    # 旧提示说的是「需要额外操作」，重连过了就该换成人话
    assert not any("需要额外操作" in w for w in result["warnings"])
    assert any("已按新参数重连" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_official_change_triggers_reconnect() -> None:
    runtime = _Runtime(platforms=["official"])
    assert await _reconnect_if_needed(runtime, _result("official.enabled")) == ["official"]
    assert runtime.called == 1


@pytest.mark.asyncio
async def test_unrelated_change_does_not_reconnect() -> None:
    runtime = _Runtime()
    assert await _reconnect_if_needed(runtime, _result("quota.per_user_per_day")) is None
    assert runtime.called == 0


@pytest.mark.asyncio
async def test_no_hook_means_no_reconnect() -> None:
    """没有 App 装配（比如 --demo / 单测里的空 runtime）时不该炸。"""
    assert await _reconnect_if_needed(object(), _result("onebot.url")) is None


@pytest.mark.asyncio
async def test_reconnect_failure_keeps_change_and_warns() -> None:
    runtime = _Runtime(boom=RuntimeError("端口被占"))
    result = _result("onebot.url")

    assert await _reconnect_if_needed(runtime, result) is None
    assert any("重连接入端失败" in w and "端口被占" in w for w in result["warnings"])
