"""服务机制：插件提供 → 内核保管 → 消费方读取；缺依赖则 blocked。

这里刻意用 `image:generate` 当服务名 —— 它只是个字符串，
内核并不认识"生图"这回事。
"""
from __future__ import annotations
from pathlib import Path
import pytest
from haiqbot.core.errors import CapabilityDenied, CapabilityUnavailable
from haiqbot.kernel.plugin import ServiceProxy
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
SERVICE = "image:generate"

PROVIDER_SRC = """
from haiqbot import Plugin, ServiceSpec


class Dummy:
    async def generate(self, prompt, *, size=None):
        return f"img:{prompt}"


class Svc(Plugin):
    id = "svc"
    name = "假服务"
    provides = {
        "image:generate": ServiceSpec(
            name="image:generate", factory="build", version=1, methods=("generate",)
        )
    }

    def build(self):
        return Dummy()
"""

CONSUMER_SRC = """
from haiqbot import Plugin


class Consumer(Plugin):
    id = "consumer"
    name = "假消费方"
    requires = ["svc"]
    services = {"image:generate": "1"}
"""

def _make_plugin(root: Path, pid: str, source: str) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(f'id = "{pid}"\n', encoding="utf-8")
    (d / "__init__.py").write_text(source, encoding="utf-8")

def _registry(runtime: Runtime, root: Path) -> Registry:
    runtime.config.plugin_dirs = [root]
    return Registry(runtime, storage_root=root.parent / "storage")

# -- 能力表（runtime 层） ---------------------------------------------------- #

def test_provide_and_withdraw(runtime: Runtime) -> None:
    runtime.provide(SERVICE, "svc-instance", "plugin-a")
    assert runtime.get_service(SERVICE) == "svc-instance"
    assert runtime.provider_owner[SERVICE] == "plugin-a"
    assert runtime.withdraw("plugin-a") == [SERVICE]
    assert runtime.get_service(SERVICE) is None
    assert SERVICE not in runtime.provider_owner
    assert runtime.withdraw("plugin-a") == []  # 幂等

def test_withdraw_only_touches_that_owner(runtime: Runtime) -> None:
    runtime.provide(SERVICE, "a", "p1")
    runtime.provide("llm:chat", "b", "p2")
    assert runtime.withdraw("p1") == [SERVICE]
    assert runtime.get_service("llm:chat") == "b"

# -- 服务代理 ---------------------------------------------------------------- #

async def test_proxy_raises_when_no_provider() -> None:
    proxy = ServiceProxy(SERVICE, lambda _n: None)
    with pytest.raises(CapabilityUnavailable):
        proxy.generate("猫")

async def test_proxy_resolves_late(runtime: Runtime) -> None:
    """先建代理、后装提供者 —— 热插拔就靠这个。"""
    proxy = ServiceProxy(SERVICE, runtime.get_service)
    with pytest.raises(CapabilityUnavailable):
        proxy.generate("猫")
    class Svc:
        async def generate(self, prompt: str, *, size: str | None = None) -> str:
            return f"img:{prompt}"
    runtime.provide(SERVICE, Svc(), "late")
    assert await proxy.generate("猫") == "img:猫"
    runtime.withdraw("late")
    with pytest.raises(CapabilityUnavailable):
        proxy.generate("猫")

def test_proxy_supports_non_async_attrs(runtime: Runtime) -> None:
    class Svc:
        max_prompt = 800
    runtime.provide(SERVICE, Svc(), "p")
    assert ServiceProxy(SERVICE, runtime.get_service).max_prompt == 800

# -- 完整链路（真实本地插件目录） -------------------------------------------- #

async def test_plugin_registers_service(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "svc", PROVIDER_SRC)
    reg = _registry(runtime, root)
    handle = await reg.load("svc")
    assert runtime.get_service(SERVICE) is not None
    assert runtime.provider_owner[SERVICE] == "svc"
    assert runtime.service_specs[SERVICE].version == 1
    assert handle.manifest.id == "svc"

async def test_unload_withdraws_service(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "svc", PROVIDER_SRC)
    reg = _registry(runtime, root)
    await reg.load("svc")
    assert runtime.get_service(SERVICE) is not None
    assert await reg.unload("svc") is True
    assert runtime.get_service(SERVICE) is None, "卸载后服务必须立刻消失"
    assert SERVICE not in runtime.service_specs

async def test_consumer_reads_service(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "svc", PROVIDER_SRC)
    _make_plugin(root, "consumer", CONSUMER_SRC)
    reg = _registry(runtime, root)
    await reg.load("svc")
    await reg.load("consumer")
    ctx = reg.handles()["consumer"].context
    assert await ctx.service(SERVICE).generate("猫") == "img:猫"

async def test_undeclared_service_is_denied(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "svc", PROVIDER_SRC)
    _make_plugin(
        root,
        "consumer",
        """
from haiqbot import Plugin


class Consumer(Plugin):
    id = "consumer"
    requires = ["svc"]
""",
    )
    reg = _registry(runtime, root)
    await reg.load("svc")
    await reg.load("consumer")
    ctx = reg.handles()["consumer"].context
    with pytest.raises(CapabilityDenied):
        ctx.service(SERVICE), "没声明 services 就必须被拒"

async def test_provider_method_missing_is_tolerated(runtime: Runtime, tmp_path: Path) -> None:
    """老写法且方法名写错：不注册服务，但不该把加载炸掉。"""
    root = tmp_path / "plugins"
    _make_plugin(
        root,
        "broken",
        """
from haiqbot import Plugin


class Broken(Plugin):
    id = "broken"
    provides = {"image:generate": "no_such_method"}
""",
    )
    reg = _registry(runtime, root)
    handle = await reg.load("broken")
    assert handle.manifest.id == "broken"
    assert runtime.get_service(SERVICE) is None

# -- 缺依赖 → blocked（第三种状态） ----------------------------------------- #

async def test_missing_dependency_blocks_instead_of_failing(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "consumer", CONSUMER_SRC)
    runtime.config.plugins_enabled = ["consumer"]
    reg = _registry(runtime, root)
    loaded = await reg.load_enabled()
    assert loaded == [], "依赖不满足 → 不加载"
    assert "consumer" not in reg.handles()
    assert "consumer" in reg.blocked(), "但必须作为 blocked 被看见"
    assert SERVICE in " ".join(reg.blocked()["consumer"])
    # 不是加载错误：面板不该显示 traceback
    assert "consumer" not in reg.status_errors

async def test_blocked_resumes_after_provider_appears(runtime: Runtime, tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    _make_plugin(root, "svc", PROVIDER_SRC)
    _make_plugin(root, "consumer", CONSUMER_SRC)
    runtime.config.plugins_enabled = ["consumer"]
    reg = _registry(runtime, root)
    assert await reg.load_enabled() == []
    assert "consumer" in reg.blocked()
    # 补上提供方（模拟面板里把 svc 启用）
    await reg.load("svc")
    resumed = await reg.resume_blocked()
    assert resumed == ["consumer"]
    assert "consumer" in reg.handles()
    assert reg.blocked() == {}

async def test_self_provided_service_satisfies_own_dependency(runtime: Runtime, tmp_path: Path) -> None:
    """painter/chat 这种"自己提供自己消费"的插件不能把自己判成缺依赖。"""
    root = tmp_path / "plugins"
    _make_plugin(
        root,
        "selfy",
        """
from haiqbot import Plugin, ServiceSpec


class Selfy(Plugin):
    id = "selfy"
    provides = {
        "image:generate": ServiceSpec(
            name="image:generate", factory="build", version=1, methods=("generate",)
        )
    }
    services = {"image:generate": "1"}

    def build(self):
        class S:
            async def generate(self, prompt, *, size=None):
                return "ok"

        return S()
""",
    )
    runtime.config.plugins_enabled = ["selfy"]
    reg = _registry(runtime, root)
    assert await reg.load_enabled() == ["selfy"]
    assert reg.blocked() == {}

async def test_load_order_independent_of_config_order(runtime: Runtime, tmp_path: Path) -> None:
    """消费方写在启用清单前面也不能误判。"""
    root = tmp_path / "plugins"
    _make_plugin(root, "svc", PROVIDER_SRC)
    _make_plugin(root, "consumer", CONSUMER_SRC)
    runtime.config.plugins_enabled = ["consumer", "svc"]
    reg = _registry(runtime, root)
    loaded = await reg.load_enabled()
    assert loaded == ["svc", "consumer"], "提供方必须先加载"

async def test_provider_factory_receives_context(runtime: Runtime, tmp_path: Path) -> None:
    import sys
    root = tmp_path / "plugins"
    _make_plugin(
        root,
        "rec",
        """
from haiqbot import Plugin, SecretSlot

CAPTURED = {}


class Rec(Plugin):
    id = "rec"
    provides = {"image:generate": "build"}
    secrets = {"api_key": SecretSlot(label="K", ref="WEFT_IMAGE_API_KEY")}

    def build(self, pctx):
        CAPTURED["plugin_id"] = pctx.plugin_id
        CAPTURED["config"] = dict(pctx.config)
        CAPTURED["slot_ref"] = pctx.slot("api_key").physical_ref
        CAPTURED["slot_set"] = pctx.slot("api_key").is_set()
        CAPTURED["has_media"] = pctx.media is not None
        return object()
""",
    )
    runtime.config.plugin_dirs = [root]
    runtime.config.plugin_config["rec"] = {"api_key_ref": "WEFT_IMAGE_API_KEY"}
    reg = Registry(runtime, storage_root=tmp_path / "storage")
    await reg.load("rec")
    captured = sys.modules["haiqbot_local_rec"].CAPTURED
    assert captured["plugin_id"] == "rec"
    assert captured["config"]["api_key_ref"] == "WEFT_IMAGE_API_KEY"
    assert "has_media" in captured
