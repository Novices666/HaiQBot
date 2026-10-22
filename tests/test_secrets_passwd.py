"""`haiq secrets passwd`：口令轮换（不依赖 DPAPI 遗留文件）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from haiqbot.config import HaiqConfig, VaultCfg, WebuiCfg
from haiqbot.kernel.vault import VAULT_FILENAME, FileBackend, VaultError

OLD = "old-passphrase-0123456789"
NEW = "new-passphrase-9876543210"


def _cfg(tmp: Path, *, enc: str = "aead") -> HaiqConfig:
    return HaiqConfig(
        data_dir=tmp / "data",
        db_path=tmp / "data" / "haiq.db",
        secrets_dir=tmp / "secrets",
        vault=VaultCfg(dir=tmp / "secrets", enc=enc),
        webui=WebuiCfg(enabled=False, write_enabled=False),
    )


def test_passwd_rotates_and_never_prints_values(tmp_path: Path, monkeypatch, capsys) -> None:
    from haiqbot.cli import _secrets_passwd

    cfg = _cfg(tmp_path)
    vdir = tmp_path / "secrets"
    vdir.mkdir(parents=True)
    path = vdir / VAULT_FILENAME
    seed = FileBackend(path, enc="aead", passphrase=lambda: OLD)
    seed.set("WEFT_IMAGE_API_KEY", "sk-do-not-leak")

    # 常见路径：环境变量里就是旧口令，交互只问新口令
    monkeypatch.setenv(cfg.vault.passphrase_ref, OLD)
    answers = iter([NEW, NEW])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))

    assert _secrets_passwd(cfg, vdir) == 0
    out = capsys.readouterr().out
    assert "轮换 1 条凭据" in out
    assert "sk-do-not-leak" not in out, "口令轮换绝不能回显凭据值"
    assert OLD not in out and NEW not in out

    # 旧口令解不开
    with pytest.raises(VaultError):
        FileBackend(path, enc="aead", passphrase=lambda: OLD).get("WEFT_IMAGE_API_KEY")
    # 新口令能读，值不变
    fresh = FileBackend(path, enc="aead", passphrase=lambda: NEW)
    assert fresh.get("WEFT_IMAGE_API_KEY") == "sk-do-not-leak"
    # 旧文件留成 .bak
    assert (vdir / (VAULT_FILENAME + ".bak")).is_file()
    # 不留半成品
    assert not (vdir / (VAULT_FILENAME + ".new")).exists()


def test_passwd_rejects_wrong_old_passphrase(tmp_path: Path, monkeypatch, capsys) -> None:
    from haiqbot.cli import _secrets_passwd

    cfg = _cfg(tmp_path)
    vdir = tmp_path / "secrets"
    vdir.mkdir(parents=True)
    FileBackend(vdir / VAULT_FILENAME, enc="aead", passphrase=lambda: OLD).set("k", "v")

    monkeypatch.setenv(cfg.vault.passphrase_ref, "totally-wrong-passphrase")
    assert _secrets_passwd(cfg, vdir) == 1
    assert "旧口令打不开" in capsys.readouterr().out


def test_passwd_rejects_short_or_identical(tmp_path: Path, monkeypatch, capsys) -> None:
    from haiqbot.cli import _secrets_passwd

    cfg = _cfg(tmp_path)
    vdir = tmp_path / "secrets"
    vdir.mkdir(parents=True)
    FileBackend(vdir / VAULT_FILENAME, enc="aead", passphrase=lambda: OLD).set("k", "v")
    monkeypatch.setenv(cfg.vault.passphrase_ref, OLD)

    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "short")
    assert _secrets_passwd(cfg, vdir) == 1
    assert "太短" in capsys.readouterr().out

    answers = iter([NEW, "mismatch-passphrase-x"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    assert _secrets_passwd(cfg, vdir) == 1
    assert "两次输入不一致" in capsys.readouterr().out

    monkeypatch.setattr("getpass.getpass", lambda *a, **k: OLD)
    assert _secrets_passwd(cfg, vdir) == 1
    assert "新旧口令相同" in capsys.readouterr().out


def test_passwd_refuses_plaintext_mode(tmp_path: Path, capsys) -> None:
    from haiqbot.cli import _secrets_passwd

    cfg = _cfg(tmp_path, enc="none")
    vdir = tmp_path / "secrets"
    vdir.mkdir(parents=True)
    (vdir / VAULT_FILENAME).write_text("{}", encoding="utf-8")
    assert _secrets_passwd(cfg, vdir) == 1
    assert "enc=" in capsys.readouterr().out


def test_passwd_prompts_old_when_env_absent(tmp_path: Path, monkeypatch) -> None:
    """环境变量没设时会交互问旧口令，再问两遍新口令。"""
    from haiqbot.cli import _secrets_passwd

    cfg = _cfg(tmp_path)
    vdir = tmp_path / "secrets"
    vdir.mkdir(parents=True)
    FileBackend(vdir / VAULT_FILENAME, enc="aead", passphrase=lambda: OLD).set("k", "v")
    monkeypatch.delenv(cfg.vault.passphrase_ref, raising=False)

    answers = iter([OLD, NEW, NEW])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    assert _secrets_passwd(cfg, vdir) == 0
    assert FileBackend(vdir / VAULT_FILENAME, enc="aead", passphrase=lambda: NEW).get("k") == "v"
