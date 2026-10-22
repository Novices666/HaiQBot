"""凭据管理接口（`/api/secrets`）。

两条底线必须钉住：
1. **列表不吐值** —— 只有单条请求才给明文，且每次写 `secret.reveal` 审计；
2. **审计里不许出现值本体** —— 面板的审计表是给人看的，泄进去等于二次泄露。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from haiqbot.config import HaiqConfig, QuotaCfg, VaultCfg, WebuiCfg
from haiqbot.webui.server import build_webui

TOKEN = "test-token-secrets-0123456789"
SECRET_VALUE = "sk-super-secret-value-9f8e7d"


class FakeVault:
    """够用的假 vault：只实现 secrets 接口真正用到的那几个方法。"""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})
        self.enc_kind = "none"
        self.primary_backend = "file"

    def get(self, ref: str) -> str | None:
        return self.values.get(ref)

    def set(self, key: str, value: str, backend: str | None = None) -> str:
        self.values[key] = value
        return "file"

    def delete(self, key: str) -> bool:
        return self.values.pop(key, None) is not None

    def keys(self) -> list[str]:
        return sorted(self.values)


class FakeAudit:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record(self, **kw: Any) -> None:
        self.calls.append(kw)

    def actions(self) -> list[str]:
        return [str(c.get("action")) for c in self.calls]

    def details(self) -> str:
        return "\n".join(str(c.get("detail")) for c in self.calls)


class FakeRegistry:
    def handles(self) -> dict[str, Any]:
        return {}


class FakeRuntime:
    def __init__(self, *, vault: FakeVault | None = None) -> None:
        self.config = HaiqConfig(
            db_path=Path("data/x.db"),
            secrets_dir=Path("secrets"),
            vault=VaultCfg(enc="none"),
            webui=WebuiCfg(enabled=True, write_enabled=True),
            quota=QuotaCfg(enabled=False),
        )
        # 面板令牌必须一直在：它是登录用的，不能被测试数据覆盖掉
        base = {"WEFT_WEBUI_TOKEN": TOKEN}
        if vault is not None:
            base.update(vault.values)
            vault.values = base
            self.vault = vault
        else:
            self.vault = FakeVault(base)
        self.audit = FakeAudit()
        self.store = None


def _client(tmp: Path, *, runtime: FakeRuntime | None = None, write_enabled: bool = True) -> TestClient:
    runtime = runtime or FakeRuntime()
    runtime.config.webui.write_enabled = write_enabled
    cfg_path = tmp / "haiq.toml"
    cfg_path.write_text("[webui]\nenabled = true\n", encoding="utf-8")
    return TestClient(build_webui(runtime, FakeRegistry(), config_path=cfg_path))


def _login(client: TestClient) -> str:
    resp = client.post("/api/auth/login", json={"token": TOKEN})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["csrf"]


# --------------------------------------------------------------------------- #


def test_list_never_leaks_values(tmp_path: Path) -> None:
    """列表只有「已设置 / 未设置」，没有值。"""
    vault = FakeVault({"WEFT_WEBUI_TOKEN": TOKEN, "WEFT_IMAGE_API_KEY": SECRET_VALUE})
    client = _client(tmp_path, runtime=FakeRuntime(vault=vault))
    _login(client)

    resp = client.get("/api/secrets")
    assert resp.status_code == 200, resp.text
    payload = resp.json()["data"]

    assert SECRET_VALUE not in resp.text, "列表接口把密钥值吐出来了"
    assert TOKEN not in resp.text

    rows = {row["key"]: row for row in payload["items"]}
    # 插件声明的槽位 + vault 里没被声明的裸键都要出现
    assert "painter.api_key" in rows
    assert "chat.api_key" in rows
    assert "WEFT_WEBUI_TOKEN" in rows
    # 同一个物理键只出现一次：被槽位声明了就不再作为裸键重复列出
    assert "WEFT_IMAGE_API_KEY" not in rows
    assert rows["painter.api_key"]["ref"] == "WEFT_IMAGE_API_KEY"
    assert rows["painter.api_key"]["set"] is True
    assert all("value" not in row for row in payload["items"])
    assert payload["vault"]["enc"] == "none"


def test_single_get_returns_value_and_audits_reveal(tmp_path: Path) -> None:
    vault = FakeVault({"WEFT_IMAGE_API_KEY": SECRET_VALUE})
    runtime = FakeRuntime(vault=vault)
    client = _client(tmp_path, runtime=runtime)
    _login(client)

    resp = client.get("/api/secrets/painter.api_key")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["value"] == SECRET_VALUE
    assert data["set"] is True

    assert "webui:secret.reveal" in runtime.audit.actions()
    # 审计里只许有键名，不许有值
    assert SECRET_VALUE not in runtime.audit.details()


def test_missing_secret_reports_unset_without_audit(tmp_path: Path) -> None:
    runtime = FakeRuntime(vault=FakeVault({}))
    client = _client(tmp_path, runtime=runtime)
    _login(client)

    data = client.get("/api/secrets/painter.api_key").json()["data"]
    assert data["set"] is False
    assert data["value"] is None
    assert "webui:secret.reveal" not in runtime.audit.actions()


def test_write_then_delete(tmp_path: Path) -> None:
    vault = FakeVault({})
    runtime = FakeRuntime(vault=vault)
    client = _client(tmp_path, runtime=runtime)
    csrf = _login(client)

    resp = client.post(
        "/api/secrets",
        json={"key": "painter.api_key", "value": SECRET_VALUE},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert resp.status_code == 200, resp.text
    assert vault.values["WEFT_IMAGE_API_KEY"] == SECRET_VALUE
    assert "webui:secret.set" in runtime.audit.actions()
    assert SECRET_VALUE not in runtime.audit.details(), "审计里出现了密钥值"

    resp = client.delete("/api/secrets/painter.api_key", headers={"X-Haiq-CSRF": csrf})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["removed"] is True
    assert "WEFT_IMAGE_API_KEY" not in vault.values


def test_write_accepts_raw_key_name(tmp_path: Path) -> None:
    """裸键名（没有声明的槽位）也要能写，否则面板管不到自己的令牌。"""
    vault = FakeVault({})
    client = _client(tmp_path, runtime=FakeRuntime(vault=vault))
    csrf = _login(client)

    resp = client.post(
        "/api/secrets",
        json={"key": "SOME_RAW_KEY", "value": "v1"},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert resp.status_code == 200, resp.text
    assert vault.values["SOME_RAW_KEY"] == "v1"


def test_empty_value_rejected(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)
    resp = client.post(
        "/api/secrets",
        json={"key": "painter.api_key", "value": "  "},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_write_requires_csrf(tmp_path: Path) -> None:
    client = _client(tmp_path)
    _login(client)
    resp = client.post("/api/secrets", json={"key": "k", "value": "v"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf"


def test_readonly_mode_blocks_write(tmp_path: Path) -> None:
    client = _client(tmp_path, write_enabled=False)
    csrf = _login(client)
    resp = client.post(
        "/api/secrets",
        json={"key": "k", "value": "v"},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "readonly"
    # 读接口不受影响
    assert client.get("/api/secrets").status_code == 200


def test_passwd_rejects_short_or_same(tmp_path: Path) -> None:
    client = _client(tmp_path)
    csrf = _login(client)

    short = client.post(
        "/api/secrets/passwd",
        json={"old": "x" * 20, "new": "short"},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert short.status_code == 400

    same = client.post(
        "/api/secrets/passwd",
        json={"old": "same-passphrase-1", "new": "same-passphrase-1"},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert same.status_code == 400


def test_passwd_refuses_plaintext_vault(tmp_path: Path) -> None:
    """enc="none" 时没有口令可换，要明确拒绝而不是假装成功。"""
    client = _client(tmp_path)
    csrf = _login(client)
    resp = client.post(
        "/api/secrets/passwd",
        json={"old": "x" * 20, "new": "y" * 20},
        headers={"X-Haiq-CSRF": csrf},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "plaintext_vault"
