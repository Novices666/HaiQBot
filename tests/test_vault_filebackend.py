"""统一文件后端的测试：JSON 信封 + enc=aead/none，所有平台同一套。"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from haiqbot.config import HaiqConfig, VaultCfg
from haiqbot.kernel.vault import FileBackend, Vault, VaultError

PASS = "correct horse battery staple"


def _backend(path: Path, *, enc: str = "aead", pw: str | None = PASS) -> FileBackend:
    return FileBackend(path, enc=enc, passphrase=lambda: pw)


def test_aead_roundtrip_and_envelope(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    b = _backend(path)
    b.set("WEFT_IMAGE_API_KEY", "sk-abcdefghijklmnop")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["v"] == 2
    assert envelope["enc"] == "aead"
    assert envelope["kdf"]["algo"] == "scrypt"
    # 密文里不能出现明文
    assert "sk-abcdefghijklmnop" not in path.read_text(encoding="utf-8")
    # 换一个实例也能读回（说明不依赖进程内缓存）
    assert _backend(path).get("WEFT_IMAGE_API_KEY") == "sk-abcdefghijklmnop"


def test_wrong_passphrase_raises(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    _backend(path).set("K", "v")
    with pytest.raises(VaultError):
        _backend(path, pw="wrong").get("K")


def test_missing_passphrase_raises_with_hint(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    _backend(path).set("K", "v")
    with pytest.raises(VaultError) as exc:
        _backend(path, pw=None).get("K")
    assert "WEFT_VAULT_PASSPHRASE" in str(exc.value)


def test_cache_invalidated_by_external_write(tmp_path: Path) -> None:
    """另一个进程/实例写过文件后，同一实例要能看到新值（热生效的基础）。"""
    path = tmp_path / "vault.json"
    first = _backend(path)
    first.set("K", "v1")
    assert first.get("K") == "v1"
    # 模拟另一个进程写入
    _backend(path).set("K2", "v2")
    assert first.get("K2") == "v2"


def test_passphrase_is_resolved_lazily(tmp_path: Path) -> None:
    """密码来源是 callable：解密前才取，所以换了环境变量后新实例立刻生效。"""
    path = tmp_path / "vault.json"
    _backend(path).set("K", "v")
    holder: dict[str, str | None] = {"pw": None}
    lazy = FileBackend(path, enc="aead", passphrase=lambda: holder["pw"])
    with pytest.raises(VaultError):
        lazy.get("K")
    holder["pw"] = PASS
    assert lazy.get("K") == "v"


def test_plain_mode_is_base64_json(tmp_path: Path) -> None:
    """enc=none 只是 base64（一层），任何人 base64 -d 就能看到 —— 这是"显式降级"的定义。"""
    path = tmp_path / "vault.json"
    _backend(path, enc="none").set("K", "secret-value")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["enc"] == "none"
    decoded = json.loads(base64.b64decode(envelope["data"]))
    assert decoded["K"] == "secret-value"


def test_legacy_v1_file_reports_migration(tmp_path: Path) -> None:
    """读到旧版 DPAPI 文件时给出可执行的迁移提示，而不是含糊的解析错误。"""
    path = tmp_path / "vault.json"
    path.write_text(json.dumps({"v": 1, "backend": "dpapi", "data": "AQAA"}), encoding="utf-8")
    with pytest.raises(VaultError) as exc:
        _backend(path).get("K")
    assert "reencrypt" in str(exc.value)


def test_unknown_enc_rejected(tmp_path: Path) -> None:
    with pytest.raises(VaultError):
        FileBackend(tmp_path / "v.json", enc="rot13")


def test_resolve_reports_source_without_value(tmp_path: Path) -> None:
    vault = Vault([_backend(tmp_path / "vault.json")])
    vault.set("PRESENT", "value")
    got = vault.resolve("PRESENT")
    assert got.present is True
    assert got.backend == "file"
    assert not hasattr(got, "value")
    missing = vault.resolve("ABSENT")
    assert missing.present is False
    assert missing.backend is None


def test_from_config_uses_vault_section(tmp_path: Path) -> None:
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none"),
    )
    vault = Vault.from_config(cfg)
    assert vault.enc_kind == "none"
    assert vault.primary_backend == "file"
    vault.set("K", "v")
    assert (tmp_path / "secrets" / "vault.json").is_file()
    assert Vault.from_config(cfg).get("K") == "v"


def test_from_config_honours_dir_override(tmp_path: Path) -> None:
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none", dir=tmp_path / "elsewhere"),
    )
    Vault.from_config(cfg).set("K", "v")
    assert (tmp_path / "elsewhere" / "vault.json").is_file()
    assert not (tmp_path / "secrets" / "vault.json").exists()


def test_write_is_independent_of_other_backends(tmp_path: Path) -> None:
    """env 只在读时优先；写永远落到 file。"""
    vault = Vault([_backend(tmp_path / "vault.json")])
    assert vault.set("K", "from-vault") == "file"
    assert _backend(tmp_path / "vault.json").get("K") == "from-vault"
