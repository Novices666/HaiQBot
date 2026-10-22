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
