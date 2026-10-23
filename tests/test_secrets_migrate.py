"""旧格式（DPAPI vault.bin）→ 统一 JSON 信封的迁移测试。

只在 Windows 上跑：造旧文件需要真实的 DPAPI 加密。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from haiqbot.config import HaiqConfig, VaultCfg
from haiqbot.kernel.vault import DpapiFileBackend, Vault

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="造 DPAPI 旧文件需要 Windows")

PASS = "migration-test-pass"


def _cfg(tmp_path: Path, *, enc: str = "aead") -> HaiqConfig:
    return HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc=enc),
    )


def _make_legacy(secrets_dir: Path) -> None:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    old = DpapiFileBackend(secrets_dir / "vault.bin")
    old.set("HAIQ_IMAGE_API_KEY", "sk-legacy-image-key-0001")
    old.set("HAIQ_CHAT_API_KEY", "sk-legacy-chat-key-0002")


def test_reencrypt_moves_dpapi_to_aead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from haiqbot.cli import _secrets_reencrypt

    cfg = _cfg(tmp_path)
    secrets_dir = tmp_path / "secrets"
    _make_legacy(secrets_dir)
    monkeypatch.setenv("HAIQ_VAULT_PASSPHRASE", PASS)

    assert _secrets_reencrypt(cfg, secrets_dir) == 0

    assert (secrets_dir / "vault.json").is_file()
    assert (secrets_dir / "vault.bin.bak").is_file()
    assert not (secrets_dir / "vault.bin").exists()

    vault = Vault.from_config(cfg)
    assert vault.enc_kind == "aead"
    assert vault.get("HAIQ_IMAGE_API_KEY") == "sk-legacy-image-key-0001"
    assert vault.get("HAIQ_CHAT_API_KEY") == "sk-legacy-chat-key-0002"


def test_reencrypt_refuses_to_overwrite_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from haiqbot.cli import _secrets_reencrypt

    cfg = _cfg(tmp_path)
    secrets_dir = tmp_path / "secrets"
    _make_legacy(secrets_dir)
    (secrets_dir / "vault.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HAIQ_VAULT_PASSPHRASE", PASS)

    assert _secrets_reencrypt(cfg, secrets_dir) == 1
    # 原有文件不能被破坏
    assert (secrets_dir / "vault.json").read_text(encoding="utf-8") == "{}"


def test_reencrypt_without_legacy_file(tmp_path: Path) -> None:
    from haiqbot.cli import _secrets_reencrypt

    assert _secrets_reencrypt(_cfg(tmp_path), tmp_path / "secrets") == 1


def test_reencrypt_to_plain_mode(tmp_path: Path) -> None:
    """enc=none 时不需要密码，也能完成迁移。"""
    from haiqbot.cli import _secrets_reencrypt

    cfg = _cfg(tmp_path, enc="none")
    secrets_dir = tmp_path / "secrets"
    _make_legacy(secrets_dir)

    assert _secrets_reencrypt(cfg, secrets_dir) == 0
    vault = Vault.from_config(cfg)
    assert vault.enc_kind == "none"
    assert vault.get("HAIQ_IMAGE_API_KEY") == "sk-legacy-image-key-0001"
