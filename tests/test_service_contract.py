"""插件之间的服务契约：声明、校验、加载顺序、边界。

区分两个概念（这是本文件的核心）：
- **能力**（image/llm/moderation…）：内核定义，消费者可选、缺了能降级；
- **服务**（economy、quiz…）：插件定义，硬依赖，缺了**这个插件就不加载**。
"""
from __future__ import annotations
import textwrap
from pathlib import Path
from typing import Any
import pytest
from haiqbot.core.errors import CapabilityDenied, CapabilityUnavailable, ContractError
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
PROVIDER = """
from haiqbot import Plugin
from haiqbot.kernel.plugin import ServiceSpec


class Economy:
    def add(self, uid, amount):
        return amount

    def balance(self, uid):
        return 100


class EconomyPlugin(Plugin):
    id = "economy"
    provides = {
        "economy": ServiceSpec(
            name="economy", factory="build", version=1, methods=("add", "balance")
        )
    }

    def build(self):
        return Economy()
"""

CONSUMER = """
from haiqbot import Plugin


class Spender(Plugin):
    id = "spender"
    requires = ["economy"]
    services = {"economy": "1"}

    def __init__(self):
        super().__init__()
        self.seen = None

    async def on_load(self, ctx):
        # 服务依赖在 on_load 之前已被核对过，这里可以放心直接用
        self.seen = ctx.service("economy").balance("u1")
"""

def _mk_plugin(base: Path, pid: str, body: str, *, version: str = "0.1.0") -> Path:
    d = base / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.toml").write_text(f'id = "{pid}"\nversion = "{version}"\n', encoding="utf-8")
    (d / "__init__.py").write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return d

@pytest.fixture
def plugins_dir(tmp_path: Path) -> Path:
    base = tmp_path / "ext"
    base.mkdir()
    return base

def _registry(runtime: Runtime, base: Path, enabled: list[str]) -> Registry:
    runtime.config.plugin_dirs = [base]
    runtime.config.plugins_enabled = enabled
    return Registry(runtime, storage_root=base.parent / "storage")

# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #

async def test_dependency_loads_first_and_is_usable(runtime: Runtime, plugins_dir: Path) -> None:
    _mk_plugin(plugins_dir, "economy", PROVIDER)
    _mk_plugin(plugins_dir, "spender", CONSUMER)
    # 故意把消费方写在前面：拓扑排序要把它纠正过来
    reg = _registry(runtime, plugins_dir, ["spender", "economy"])
    loaded = await reg.load_enabled()
    assert loaded == ["economy", "spender"], "提供方必须先加载"
    spender = reg.handles()["spender"]
    assert spender.plugin.seen == 100, "ctx.service() 应该拿到真实实例"
    assert "economy" in runtime.service_specs
    assert runtime.service_specs["economy"].version == 1

async def test_proxy_follows_withdraw(runtime: Runtime, plugins_dir: Path) -> None:
    """提供方被卸载后，缓存的代理要立刻失效 —— 否则热插拔就是假的。"""
    _mk_plugin(plugins_dir, "economy", PROVIDER)
    _mk_plugin(plugins_dir, "spender", CONSUMER)
    reg = _registry(runtime, plugins_dir, ["economy", "spender"])
    await reg.load_enabled()
    proxy = reg.handles()["spender"].context.service("economy")
    assert proxy.balance("u1") == 100
    await reg.unload("economy")
    with pytest.raises(CapabilityUnavailable):
        proxy.balance("u1")

# --------------------------------------------------------------------------- #
# 硬约束：缺服务就不加载
# --------------------------------------------------------------------------- #

async def test_missing_service_blocks_load(runtime: Runtime, plugins_dir: Path) -> None:
    _mk_plugin(plugins_dir, "spender", CONSUMER)
    reg = _registry(runtime, plugins_dir, ["spender"])
    loaded = await reg.load_enabled()
    assert loaded == []
    blocked = reg.blocked()
    assert "spender" in blocked
    assert "economy" in " ".join(blocked["spender"])

async def test_version_mismatch_blocks_load(runtime: Runtime, plugins_dir: Path) -> None:
    _mk_plugin(plugins_dir, "economy", PROVIDER)
    _mk_plugin(
        plugins_dir,
        "spender",
        CONSUMER.replace('services = {"economy": "1"}', 'services = {"economy": ">=2"}'),
    )
    reg = _registry(runtime, plugins_dir, ["economy", "spender"])
    loaded = await reg.load_enabled()
    assert loaded == ["economy"]
    blocked = reg.blocked()
    assert "spender" in blocked
    assert "需要 v2" in " ".join(blocked["spender"])

async def test_contract_method_missing_blocks_provide(runtime: Runtime, plugins_dir: Path) -> None:
    """契约写了 balance，实现里没有 → 提供方自己加载失败，而不是让消费方运行时炸。"""
    bad = PROVIDER.replace('methods=("add", "balance")', 'methods=("add", "balance", "transfer")')
    _mk_plugin(plugins_dir, "economy", bad)
    reg = _registry(runtime, plugins_dir, ["economy"])
    assert await reg.load_enabled() == []
    err = next(r for r in reg.status() if r["id"] == "economy")["error"]
    assert "缺少契约方法" in err and "transfer" in err

async def test_service_without_contract_is_reported_precisely(runtime: Runtime, plugins_dir: Path) -> None:
    """老写法（provides 只给方法名）没有契约：消费方会得到"没有声明契约"的提示。"""
    legacy = PROVIDER.replace(
        """provides = {
        "economy": ServiceSpec(
            name="economy", factory="build", version=1, methods=("add", "balance")
        )
    }""",
        'provides = {"economy": "build"}',
    )
    _mk_plugin(plugins_dir, "economy", legacy)
    _mk_plugin(plugins_dir, "spender", CONSUMER)
    reg = _registry(runtime, plugins_dir, ["economy", "spender"])
    await reg.load_enabled()
    blocked = reg.blocked()
    assert "spender" in blocked
    assert "没有声明契约" in " ".join(blocked["spender"])

# --------------------------------------------------------------------------- #
# 边界：没声明就不给
# --------------------------------------------------------------------------- #

async def test_undeclared_service_is_denied(runtime: Runtime, plugins_dir: Path) -> None:
    greedy = CONSUMER.replace('    services = {"economy": "1"}\n', "")
    _mk_plugin(plugins_dir, "economy", PROVIDER)
    _mk_plugin(plugins_dir, "spender", greedy)
    reg = _registry(runtime, plugins_dir, ["economy", "spender"])
    assert await reg.load_enabled() == ["economy"]
    err = next(r for r in reg.status() if r["id"] == "spender")["error"]
    assert "没有声明服务" in err

def test_service_denied_error_type() -> None:
    """边界错误的类型要对：越权 = CapabilityDenied，不可用 = CapabilityUnavailable。"""
    assert issubclass(CapabilityDenied, Exception)
    assert not issubclass(CapabilityDenied, CapabilityUnavailable)

# --------------------------------------------------------------------------- #
# 依赖成环
# --------------------------------------------------------------------------- #

async def test_cycle_is_reported_not_silently_loaded(runtime: Runtime, plugins_dir: Path) -> None:
    a = """
    from haiqbot import Plugin

    class A(Plugin):
        id = "a"
        requires = ["b"]
    """
    b = """
    from haiqbot import Plugin

    class B(Plugin):
        id = "b"
        requires = ["a"]
    """
    _mk_plugin(plugins_dir, "a", a)
    _mk_plugin(plugins_dir, "b", b)
    reg = _registry(runtime, plugins_dir, ["a", "b"])
    with pytest.raises(ContractError) as exc:
        reg._topo_sort(["a", "b"])
    assert "成环" in str(exc.value)
    # 启动时不能因此把整个 bot 打死，但必须留下可见的错误
    loaded = await reg.load_enabled()
    assert sorted(loaded) == ["a", "b"]
    assert "__dependencies__" in reg.status_errors

def _noop(*_a: Any) -> None:
    pass
