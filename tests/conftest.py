"""测试夹具。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from haiqbot.config import HaiqConfig, MediaCfg, PolicyCfg, QuotaCfg, WebuiCfg
from haiqbot.kernel.bus import Bus
from haiqbot.kernel.governance import AuditLog, Idempotency, QuotaService, RoleResolver
from haiqbot.kernel.runtime import Runtime
from haiqbot.kernel.store import Store
from haiqbot.kernel.vault import EnvBackend, PlainFileBackend, Vault


@pytest.fixture
def cfg(tmp_path: Path) -> HaiqConfig:
    """完全隔离的临时配置：独立 data/db/media/secrets，关闭 webui。"""
    return HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        plugins_enabled=["echo"],
        # 测试同时覆盖「框架最小集」与「个人集成」两个插件目录，
        # 和仓库根 haiq.toml 的 plugin_dirs 保持一致。
        plugin_dirs=[Path("plugins"), Path("plugins-extra")],
        quota=QuotaCfg(enabled=True, per_user_per_day=5, per_channel_per_day=10, rate_per_user_per_min=60),
        policy=PolicyCfg(handle_timeout_seconds=2.0, max_handlers_per_event=1),
        media=MediaCfg(dir=tmp_path / "data" / "media", ttl_seconds=3600),
        webui=WebuiCfg(enabled=False),
    )


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    """测试用凭据库：env + 明文文件后端（不依赖 DPAPI 用户绑定）。"""
    return Vault([EnvBackend(), PlainFileBackend(tmp_path / "secrets" / "vault.json")])


@pytest.fixture
async def store(cfg: HaiqConfig) -> AsyncIterator[Store]:
    s = await Store(cfg.db_path).open()
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
async def runtime(cfg: HaiqConfig, store: Store, vault: Vault) -> Runtime:
    resolver = RoleResolver.from_policy(cfg.policy)
    return Runtime(
        config=cfg,
        store=store,
        vault=vault,
        bus=Bus(timeout=cfg.policy.handle_timeout_seconds),
        resolver=resolver,
        quota=QuotaService(store, cfg.quota, resolver),
        idempotency=Idempotency(store),
        audit=AuditLog(store),
    )


# ---------------------------------------------------------------- 插件预加载
# 插件已经搬出内核（不再是 pip entry point），测试要像运行时那样按「本地目录」
# 协议把它们 import 成 `haiqbot_local_<pid>`，否则顶层的 import 语句会失败。
REPO_ROOT = Path(__file__).resolve().parent.parent
# 两个目录都要预加载：`plugins/`（框架最小集）+ `plugins-extra/`（自有插件），
# 与仓库根 haiq.toml 的 plugin_dirs 保持一致。
PLUGIN_DIRS = [REPO_ROOT / "plugins", REPO_ROOT / "plugins-extra"]


def _preload_local_plugins() -> list[str]:
    from haiqbot.kernel.plugin_sources import load_local_module

    loaded: list[str] = []
    for base in PLUGIN_DIRS:
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            if not (child / "plugin.toml").is_file() or not (child / "__init__.py").is_file():
                continue
            load_local_module(child.name, child)
            loaded.append(child.name)
    return loaded


PRELOADED_PLUGINS = _preload_local_plugins()
