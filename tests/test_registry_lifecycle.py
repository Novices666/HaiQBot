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
    assert "assistant" in found
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
    assistant = await registry.load("assistant")
    # assistant 自己提供两个服务也自己消费，两个句柄都必须到位
    assert "image:generate" in assistant.context.services
    assert "llm:chat" in assistant.context.services
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


class _LogSink:
    """替掉模块级 log，只为断言事件；不关心格式。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def info(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def warning(self, *_a: object, **_kw: object) -> None:
        return None

    def error(self, *_a: object, **_kw: object) -> None:
        return None

    def exception(self, *_a: object, **_kw: object) -> None:
        return None


async def test_load_enabled_reports_discovered_but_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """磁盘/入口里能发现的插件，只要没进 enabled 就永远不加载 —— 启动时说清是哪些。"""
    from haiqbot.kernel import registry as reg_mod

    sink = _LogSink()
    monkeypatch.setattr(reg_mod, "log", sink)

    class _Cfg:
        plugins_enabled: list[str] = []

    class _RT:
        config = _Cfg()

    registry = reg_mod.Registry(_RT())  # type: ignore[arg-type]
    monkeypatch.setattr(registry, "discover", lambda: {"echo": "local", "guard": "local"})

    assert await registry.load_enabled() == []
    assert ("plugins_discovered_but_disabled", {"plugins": ["echo", "guard"]}) in sink.events


async def test_load_enabled_silent_when_everything_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from haiqbot.kernel import registry as reg_mod

    sink = _LogSink()
    monkeypatch.setattr(reg_mod, "log", sink)

    class _Cfg:
        plugins_enabled: list[str] = []

    class _RT:
        config = _Cfg()

    registry = reg_mod.Registry(_RT())  # type: ignore[arg-type]
    monkeypatch.setattr(registry, "discover", dict)

    await registry.load_enabled()
    assert sink.events == []
