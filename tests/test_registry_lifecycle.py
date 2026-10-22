"""插件注册器：加载 / 卸载 / 重载 / 清单校验。"""
from __future__ import annotations
from pathlib import Path
import pytest
from haiqbot.core.errors import CapabilityDenied, HaiqError
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime

async def test_builtin_plugin_discovered_and_loaded(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    found = registry.discover()
    assert "echo" in found
    assert "painter" in found
    handle = await registry.load("echo")
    assert handle.manifest.id == "echo"
    assert handle.rules, "echo 应该注册了至少一条规则"
    assert len(runtime.bus.rules_of("echo")) == len(handle.rules)

async def test_load_then_unload_clears_rules(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    await registry.load("echo")
    assert runtime.bus.rules_of("echo")
    assert await registry.unload("echo") is True
    assert runtime.bus.rules_of("echo") == []
    assert await registry.unload("echo") is False

async def test_reload_is_idempotent(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    first = await registry.load("echo")
    second = await registry.reload("echo")
    assert isinstance(second.rules, list)
    assert len(runtime.bus.rules_of("echo")) == len(first.rules)

async def test_services_granted_by_manifest(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    echo = await registry.load("echo")
    assert echo.context.services == {}, "echo 没有声明任何服务，不该拿到句柄"
    painter = await registry.load("painter")
    # painter 自己提供 image:generate 也自己消费，句柄必须到位
    assert "image:generate" in painter.context.services
    # 越权取服务仍然是明确的错误，不是 None
    with pytest.raises(CapabilityDenied):
        echo.context.service("image:generate")

async def test_unknown_plugin_raises(runtime: Runtime) -> None:
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    with pytest.raises(HaiqError):
        await registry.load("does-not-exist")

async def test_load_enabled_isolates_failures(runtime: Runtime) -> None:
    runtime.config.plugins_enabled = ["echo", "does-not-exist"]
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    loaded = await registry.load_enabled()
    assert "echo" in loaded
    status = {row["id"]: row for row in registry.status()}
    assert status["echo"]["loaded"] is True

async def test_local_plugin_dir(tmp_path: Path, runtime: Runtime) -> None:
    """本地目录插件（plugin.toml + __init__.py）也应被发现并加载。"""
    plugin_dir = tmp_path / "localplugins" / "hello"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text('id = "hello"\nversion = "0.2.0"\n', encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(
        "from haiqbot.plugin_api import Context, Plugin, on_command\n"
        "\n"
        "class Hello(Plugin):\n"
        "    id = 'hello'\n"
        "    name = '打招呼'\n"
        "\n"
        "    @on_command('hi', usage='hi')\n"
        "    async def hi(self, ctx: Context) -> None:\n"
        "        await ctx.reply.text('hi!')\n",
        encoding="utf-8",
    )
    runtime.config.plugin_dirs = [tmp_path / "localplugins"]
    registry = Registry(runtime, storage_root=Path(runtime.config.data_dir) / "plugins")
    assert "hello" in registry.discover()
    handle = await registry.load("hello")
    assert handle.manifest.version == "0.2.0"
    assert handle.manifest.source == "local"
    assert runtime.bus.rules_of("hello")
