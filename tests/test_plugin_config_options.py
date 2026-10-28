"""动态可选值（`registry.config_options`）：模型列表这类「要现问接口」的字段。

四条约束逐条钉死（它们都是踩过的坑）：
1. 插件没实现 → `supported=False`，不是报错；
2. 面板上**还没保存**的值要能带进去（否则改个地址还得先保存一次）；
3. 抛错只变成一句人话，**且绝不带出密钥**；
4. 超时要有上限（不能把 WebUI 挂在一个不响应的网关上）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from haiqbot.kernel import registry as registry_mod
from haiqbot.kernel.plugin import SecretSlot
from haiqbot.kernel.registry import Registry


class _Vault:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = values or {}

    def get(self, key: str) -> str | None:
        return self._values.get(key)

    def resolve(self, key: str) -> Any:
        return SimpleNamespace(present=key in self._values)


def _runtime(*, plugin_config: dict[str, dict[str, Any]] | None = None, vault: Any = None) -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(plugin_config=plugin_config or {}, plugin_dirs=[]),
        vault=vault or _Vault(),
        audit=None,
        media=None,
        get_service=lambda name: None,
    )


def _registry(plugin: Any, *, runtime: Any = None) -> Registry:
    reg = Registry(runtime or _runtime())
    reg._plugin_for_options = lambda pid: plugin  # type: ignore[method-assign]
    return reg


class _NoOptions:
    """老插件：没有 config_options，不该被当成错误。"""

    id = "plain"


@pytest.mark.asyncio
async def test_plugin_without_hook_reports_unsupported():
    out = await _registry(_NoOptions()).config_options("plain", "model")
    assert out == {"plugin": "plain", "key": "model", "options": [], "supported": False}


@pytest.mark.asyncio
async def test_string_list_is_normalised_and_deduped():
    class Plug:
        id = "demo"

        def config_options(self, key: str, pctx: Any) -> list[str]:
            assert key == "model"
            return ["b", "a", "b", "  ", "c"]

    out = await _registry(Plug()).config_options("demo", "model")
    assert out["supported"] is True
    assert [item["value"] for item in out["options"]] == ["b", "a", "c"]
    assert out["options"][0] == {"value": "b", "label": "b"}


@pytest.mark.asyncio
async def test_mapping_options_keep_their_label():
    class Plug:
        id = "demo"

        async def config_options(self, key: str, pctx: Any) -> list[dict[str, str]]:
            return [{"value": "m1", "label": "模型一"}, {"value": "m2"}]

    out = await _registry(Plug()).config_options("demo", "model")
    assert out["options"] == [
        {"value": "m1", "label": "模型一"},
        {"value": "m2", "label": "m2"},
    ]


@pytest.mark.asyncio
async def test_unsaved_overrides_reach_the_plugin():
    """面板上刚敲的 base_url 必须能带进去 —— 否则「先保存才能拉列表」。"""

    class Plug:
        id = "demo"

        async def config_options(self, key: str, pctx: Any) -> list[str]:
            return [str(pctx.config.get("base_url")), str(pctx.config.get("saved"))]

    runtime = _runtime(plugin_config={"demo": {"base_url": "http://old", "saved": "keep"}})
    out = await _registry(Plug(), runtime=runtime).config_options(
        "demo", "model", {"base_url": "http://new", "evil key": "x"}
    )
    assert [item["value"] for item in out["options"]] == ["http://new", "keep"]
    # 配置本身不能被改：拉列表是只读操作
    assert runtime.config.plugin_config["demo"]["base_url"] == "http://old"


@pytest.mark.asyncio
async def test_secret_slot_is_available_but_not_leaked():
    class Plug:
        id = "demo"
        secrets = {"api_key": SecretSlot(label="示例密钥", ref="DEMO_KEY")}

        async def config_options(self, key: str, pctx: Any) -> list[str]:
            # 能取到自己的凭据（说明槽位发放是对的）
            assert pctx.slot("api_key").headers()["Authorization"].startswith("Bearer ")
            raise RuntimeError("上游 500，key=sk-live-abcdef1234567890")

    vault = _Vault({"DEMO_KEY": "sk-live-abcdef1234567890"})
    out = await _registry(Plug(), runtime=_runtime(vault=vault)).config_options("demo", "model")
    assert out["supported"] is True
    assert out["options"] == []
    assert out["error"]
    assert "sk-live-abcdef1234567890" not in out["error"]
    assert "500" in out["error"]


@pytest.mark.asyncio
async def test_timeout_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch):
    class Plug:
        id = "demo"

        async def config_options(self, key: str, pctx: Any) -> list[str]:
            await asyncio.sleep(5)
            return ["never"]

    monkeypatch.setattr(registry_mod, "_OPTIONS_TIMEOUT_SECONDS", 0.05)
    out = await _registry(Plug()).config_options("demo", "model")
    assert out["options"] == []
    assert "超时" in (out["error"] or "")


@pytest.mark.asyncio
async def test_empty_list_is_called_out_but_still_supported():
    class Plug:
        id = "demo"

        def config_options(self, key: str, pctx: Any) -> list[str]:
            return []

    out = await _registry(Plug()).config_options("demo", "model")
    assert out["supported"] is True
    assert out["options"] == []
    assert "空" in (out["error"] or "")


@pytest.mark.asyncio
async def test_missing_plugin_is_not_an_exception():
    reg = Registry(_runtime())

    def boom(pid: str) -> Any:
        raise KeyError(pid)

    reg._plugin_for_options = boom  # type: ignore[method-assign]
    out = await reg.config_options("ghost", "model")
    assert out["supported"] is False
    assert "不可用" in (out["error"] or "")
