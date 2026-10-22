"""凭据库与日志脱敏 —— 这是「密钥不泄露」的硬断言。"""

from __future__ import annotations

from pathlib import Path

import pytest

from haiqbot.kernel.log import (
    REDACTED,
    clear_registered_secrets,
    redact_text,
    redact_value,
    register_secret,
)
from haiqbot.kernel.plugin import Context
from haiqbot.kernel.vault import EnvBackend, PlainFileBackend, Vault, VaultError


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    clear_registered_secrets()
    yield
    clear_registered_secrets()


def test_plain_backend_roundtrip(tmp_path: Path) -> None:
    vault = Vault([PlainFileBackend(tmp_path / "vault.json")])
    vault.set("KEY_A", "value-a")
    assert vault.get("KEY_A") == "value-a"
    assert vault.keys() == ["KEY_A"]
    assert vault.delete("KEY_A") is True
    assert vault.get("KEY_A") is None


def test_env_backend_readonly() -> None:
    import os

    os.environ["WEFT_TEST_KEY"] = "from-env"
    try:
        vault = Vault([EnvBackend()])
        assert vault.get("WEFT_TEST_KEY") == "from-env"
        with pytest.raises(VaultError):
            vault.set("WEFT_TEST_KEY", "x")
    finally:
        os.environ.pop("WEFT_TEST_KEY", None)


def test_require_raises_with_hint(tmp_path: Path) -> None:
    vault = Vault([PlainFileBackend(tmp_path / "vault.json")])
    with pytest.raises(VaultError) as exc:
        vault.require("MISSING")
    assert "haiq vault set MISSING" in str(exc.value)


def test_priority_order(tmp_path: Path) -> None:
    """env 优先于文件后端。"""
    import os

    file_backend = PlainFileBackend(tmp_path / "vault.json")
    file_backend.set("DUP", "from-file")
    os.environ["DUP"] = "from-env"
    try:
        vault = Vault([EnvBackend(), file_backend])
        assert vault.get("DUP") == "from-env"
    finally:
        os.environ.pop("DUP", None)


def test_default_backend_is_same_on_every_platform(tmp_path: Path) -> None:
    """不再做平台区分：默认链在所有系统上都是 env -> file(aead)。"""
    vault = Vault.default(tmp_path / "secrets")
    assert vault.primary_backend == "file"
    assert vault.enc_kind == "aead"


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #
def test_redact_known_patterns() -> None:
    assert "sk-" not in redact_text("key=sk-abcdefghijklmnopqrstuvwxyz")
    assert "Bearer" not in redact_text("Authorization: Bearer abcdefgh12345")
    assert REDACTED in redact_text('{"clientSecret": "supersecretvalue"}')


def test_redact_registered_secret_exactly() -> None:
    secret = "my-very-private-token-123"
    register_secret(secret)
    out = redact_text(f"请求失败，附带 {secret} 与前后文")
    assert secret not in out
    assert REDACTED in out


def test_redact_nested_structure() -> None:
    register_secret("tok-abcdefgh")
    payload = {
        "headers": {"authorization": "Bearer tok-abcdefgh"},
        "api_key": "plainvalue",
        "list": ["ok", "tok-abcdefgh"],
        "nested": {"deeper": {"note": "x tok-abcdefgh"}},
    }
    out = redact_value(payload)
    assert out["headers"]["authorization"] == REDACTED  # 敏感键名整体打码
    assert out["api_key"] == REDACTED
    assert out["list"][0] == "ok"
    assert "tok-abcdefgh" not in str(out)


def test_vault_get_registers_secret(tmp_path: Path) -> None:
    vault = Vault([PlainFileBackend(tmp_path / "vault.json")])
    vault.set("K", "sk-registerme-1234567890")
    value = vault.get("K")
    assert value is not None
    # 取过之后，日志里出现就该被打码
    assert value not in redact_text(f"value={value}")


# --------------------------------------------------------------------------- #
# 结构性断言：Context 里不允许出现任何凭据字段
# --------------------------------------------------------------------------- #
def test_context_has_no_secret_fields() -> None:
    fields = set(Context.__dataclass_fields__)
    forbidden = {
        "vault",
        "secret",
        "secrets",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "app_secret",
        "client_secret",
        "password",
        "env",
        "environ",
    }
    assert fields & forbidden == set(), f"Context 暴露了不该暴露的字段：{fields & forbidden}"


def test_context_repr_has_no_secret() -> None:
    register_secret("sk-hidden-value-0001")
    ctx = Context(trace_id="t1", plugin_id="p", log=None)
    assert "sk-hidden-value-0001" not in repr(ctx)
