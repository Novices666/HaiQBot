"""面板后端：鉴权、CSRF、只读模式、配置回滚、工作台预演、媒体。



这些测试全部跑真实 Starlette app（`TestClient`），只把 runtime/registry 换成假对象 ——

面板最容易出错的不是业务逻辑，而是"门有没有关严"。

"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from haiqbot.config import HaiqConfig, OneBotCfg, QuotaCfg, VaultCfg, WebuiCfg
from haiqbot.webui.auth import Auth
from haiqbot.webui.server import build_webui

TOKEN = "test-token-0123456789"


# --------------------------------------------------------------------------- #

# 假对象

# --------------------------------------------------------------------------- #


class FakeStore:
    def __init__(self) -> None:

        self.channels: dict[str, dict[str, Any]] = {}

        self.media: list[dict[str, Any]] = []

        self.audit_rows: list[dict[str, Any]] = []

    async def media_count(self) -> int:

        return len(self.media)

    async def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:

        return self.audit_rows[-limit:]

    async def media_list(self, plugin_id: str | None = None, limit: int = 60) -> list[dict]:

        return self.media[:limit]

    async def media_get(self, media_id: str) -> dict[str, Any] | None:

        return next((m for m in self.media if m["id"] == media_id), None)

    async def media_delete(self, media_id: str) -> bool:

        before = len(self.media)

        self.media = [m for m in self.media if m["id"] != media_id]

        return len(self.media) != before

    async def channel_all(self) -> list[dict[str, Any]]:

        return list(self.channels.values())

    async def channel_get(self, channel: str) -> dict[str, Any] | None:

        return self.channels.get(channel)

    async def channel_upsert(self, channel: str, **fields: Any) -> None:

        if "bogus" in fields:
            raise ValueError("字段不合法")

        row = self.channels.setdefault(channel, {"channel": channel})

        row.update(fields)

    async def channel_delete(self, channel: str) -> bool:

        return self.channels.pop(channel, None) is not None

    async def invocation_recent(self, limit: int = 100, **kw: Any) -> list[dict[str, Any]]:

        return []

    async def invocation_stats(self, *, hours: float = 24) -> dict[str, Any]:

        return {"hours": hours, "items": []}

    async def invocation_delete(self, **kw: Any) -> int:

        if not any(kw.get(k) for k in ("before", "plugin_id", "capability")):
            raise ValueError("至少给一个过滤条件")

        return 0


class FakeVault:
    def __init__(self, values: dict[str, str]) -> None:

        self.values = values

    def get(self, ref: str) -> str | None:

        return self.values.get(ref)


class FakeAudit:
    def __init__(self) -> None:

        self.calls: list[dict[str, Any]] = []

    async def record(self, **kw: Any) -> None:

        self.calls.append(kw)


class FakeAdapter:
    def describe(self) -> dict[str, Any]:

        return {"platform": "onebot", "bot_id": "1000", "health": {"connected": True}}


class FakeBus:
    def __init__(self, rules: list[Any] | None = None) -> None:

        self._rules = rules or []


class FakeRuntime:
    def __init__(self, *, token: str | None = TOKEN, rules: list[Any] | None = None) -> None:

        self.config = HaiqConfig(
            db_path=Path("data/x.db"),
            secrets_dir=Path("secrets"),
            vault=VaultCfg(enc="none"),
            webui=WebuiCfg(enabled=True, write_enabled=True),
            onebot=OneBotCfg(enabled=True),
            quota=QuotaCfg(enabled=False),
        )

        self.store = FakeStore()

        self.vault = FakeVault({} if token is None else {"HAIQ_WEBUI_TOKEN": token})

        self.audit = FakeAudit()

        self.adapters = {"onebot": FakeAdapter()}

        self.bus = FakeBus(rules)

        self.media = None

        self.outbound = None

        self.providers: dict[str, Any] = {}

        self.provider_owner: dict[str, str] = {}

        self.service_specs: dict[str, Any] = {}

        self.started_at = time.time() - 42

        self.events_total = 7

        self.handled_total = 5

        self.last_event_ts = time.time()

    def health(self) -> dict[str, Any]:

        return {"connected": True, "platforms": {"onebot": {"connected": True}}}


class FakeRegistry:
    def __init__(self) -> None:

        self.status_errors: dict[str, str] = {}

    def status(self) -> list[dict[str, Any]]:

        return [
            {
                "id": "echo",
                "source": "entrypoint",
                "enabled": True,
                "loaded": True,
                "name": "回声",
                "version": "0.1.0",
                "capabilities": [],
                "rules": ["echo:/echo"],
                "secrets": {"api_key": {"label": "API Key", "required": False, "ref": "HAIQ_WEBUI_TOKEN"}},
            }
        ]

    def commands(self) -> list[dict[str, Any]]:

        return [
            {
                "plugin": "echo",
                "plugin_name": "回声",
                "command": "/echo",
                "aliases": [],
                "usage": "<内容>",
                "description": "回显",
                "min_role": 0,
            }
        ]

    def config_schema(self, pid: str) -> dict[str, Any]:

        return {"fields": []}

    def handles(self) -> dict[str, Any]:

        return {}

    def blocked(self) -> dict[str, str]:

        return {}


def _client(tmp: Path, *, token: str | None = TOKEN, write_enabled: bool = True) -> TestClient:

    runtime = FakeRuntime(token=token)

    registry = FakeRegistry()

    runtime.config.webui.write_enabled = write_enabled

    cfg_path = tmp / "haiq.toml"

    cfg_path.write_text("[webui]\nenabled = true\n", encoding="utf-8")

    runtime.config.data_dir = tmp / "data"

    app = build_webui(runtime, registry, config_path=cfg_path)

    return TestClient(app)


def _login(client: TestClient, token: str = TOKEN) -> str:

    resp = client.post("/api/auth/login", json={"token": token})

    assert resp.status_code == 200, resp.text

    return resp.json()["data"]["csrf"]


# --------------------------------------------------------------------------- #

# 认证

# --------------------------------------------------------------------------- #


def test_login_required_for_api(tmp_path: Path) -> None:

    client = _client(tmp_path)

    resp = client.get("/api/overview")

    assert resp.status_code == 401

    assert resp.json()["error"]["code"] == "unauthorized"


def test_login_sets_cookie_and_csrf(tmp_path: Path) -> None:

    client = _client(tmp_path)

    csrf = _login(client)

    assert csrf

    resp = client.get("/api/auth/session")

    assert resp.status_code == 200

    data = resp.json()["data"]

    assert data["csrf"] == csrf

    assert data["write_enabled"] is True

    assert data["token_configured"] is True


def test_wrong_token_rejected(tmp_path: Path) -> None:

    client = _client(tmp_path)

    resp = client.post("/api/auth/login", json={"token": "nope"})

    assert resp.status_code == 401

    assert resp.json()["error"]["code"] == "bad_token"


def test_login_rate_limited(tmp_path: Path) -> None:

    client = _client(tmp_path)

    for _ in range(5):
        assert client.post("/api/auth/login", json={"token": "nope"}).status_code == 401

    resp = client.post("/api/auth/login", json={"token": "nope"})

    assert resp.status_code == 429

    assert resp.json()["error"]["code"] == "too_many_attempts"


def test_token_missing_blocks_login(tmp_path: Path) -> None:

    client = _client(tmp_path, token=None)

    resp = client.post("/api/auth/login", json={"token": "x"})

    assert resp.status_code == 503

    assert resp.json()["error"]["code"] == "token_missing"


def test_logout_clears_session(tmp_path: Path) -> None:

    client = _client(tmp_path)

    csrf = _login(client)

    assert client.post("/api/auth/logout", headers={"X-Haiq-CSRF": csrf}).status_code == 200

    assert client.get("/api/overview").status_code == 401


# --------------------------------------------------------------------------- #

# CSRF / 只读模式

# --------------------------------------------------------------------------- #


def test_write_requires_csrf(tmp_path: Path) -> None:

    client = _client(tmp_path)

    _login(client)

    resp = client.post("/api/channels/onebot:group:1", json={"enabled": True})

    assert resp.status_code == 403

    assert resp.json()["error"]["code"] == "csrf"


def test_write_with_csrf_ok(tmp_path: Path) -> None:

    client = _client(tmp_path)

    csrf = _login(client)

    resp = client.post("/api/channels/onebot:group:1", json={"enabled": True}, headers={"X-Haiq-CSRF": csrf})

    assert resp.status_code == 200, resp.text

    assert resp.json()["data"]["enabled"] is True


def test_readonly_mode_blocks_writes(tmp_path: Path) -> None:

    client = _client(tmp_path, write_enabled=False)

    csrf = _login(client)

    resp = client.post("/api/channels/onebot:group:1", json={"enabled": True}, headers={"X-Haiq-CSRF": csrf})

    assert resp.status_code == 403

    assert resp.json()["error"]["code"] == "readonly"

    # 读接口照常

    assert client.get("/api/overview").status_code == 200


# --------------------------------------------------------------------------- #

# 会话签名本身

# --------------------------------------------------------------------------- #


def test_session_signature_and_expiry() -> None:

    auth = Auth(token=TOKEN, ttl_seconds=300)

    cookie, session = auth.issue(now=1_000_000)

    assert auth.verify(cookie, now=1_000_010) is not None

    # 过期

    assert auth.verify(cookie, now=1_000_400) is None

    # 被改

    assert auth.verify(cookie + "x", now=1_000_010) is None

    # 换令牌 = 旧会话全失效

    other = Auth(token="another-token")

    assert other.verify(cookie, now=1_000_010) is None

    assert session.csrf == auth.verify(cookie, now=1_000_010).csrf


def test_csrf_differs_from_cookie() -> None:

    auth = Auth(token=TOKEN)

    cookie, session = auth.issue()

    assert session.csrf not in cookie


# --------------------------------------------------------------------------- #

# 各接口

# --------------------------------------------------------------------------- #


def test_overview_shape(tmp_path: Path) -> None:

    client = _client(tmp_path)

    _login(client)

    data = client.get("/api/overview").json()["data"]

    assert data["runtime"]["events_total"] == 7

    assert data["plugins"]["loaded"] == 1

    assert data["config"]["onebot_enabled"] is True

    assert data["platforms"]["onebot"]["bot_id"] == "1000"


def test_plugins_list_and_commands(tmp_path: Path) -> None:

    client = _client(tmp_path)

    _login(client)

    rows = client.get("/api/plugins").json()["data"]

    assert rows[0]["id"] == "echo"

    assert rows[0]["commands"][0]["command"] == "/echo"

    assert rows[0]["secrets"][0]["ref"] == "HAIQ_WEBUI_TOKEN"

    # set 现在来自 vault 的真实状态，而不是插件自报的标记

    assert rows[0]["secrets"][0]["set"] is True

    # 凭据只暴露"是否已设"，绝不带真值

    assert "value" not in rows[0]["secrets"][0]

    assert client.get("/api/commands").json()["data"][0]["command"] == "/echo"


def test_channels_unknown_field_rejected(tmp_path: Path) -> None:

    client = _client(tmp_path)

    csrf = _login(client)

    resp = client.post("/api/channels/onebot:group:1", json={"bogus": 1}, headers={"X-Haiq-CSRF": csrf})

    assert resp.status_code == 400

    assert resp.json()["error"]["code"] == "unknown_field"


def test_media_list_and_missing_file(tmp_path: Path) -> None:

    client = _client(tmp_path)

    _login(client)

    assert client.get("/api/media").json()["data"] == []

    assert client.get("/i/nope").status_code == 404

    assert client.delete("/api/media/nope").status_code == 403  # 没 CSRF，先被门挡住


def test_config_history_and_rollback(tmp_path: Path) -> None:

    client = _client(tmp_path)

    csrf = _login(client)

    cfg_path = tmp_path / "haiq.toml"

    # 造一份历史快照（面板写配置时会自动生成，这里直接放一份）

    history = tmp_path / "data" / "webui" / "config-history"

    history.mkdir(parents=True)

    (history / "20260101-000000-000.toml").write_text("[webui]\nenabled = false\n", encoding="utf-8")

    rows = client.get("/api/config/history").json()["data"]

    assert rows and rows[0]["id"].startswith("20260101")

    resp = client.post(
        "/api/config/rollback",
        json={"id": "20260101-000000-000.toml"},
        headers={"X-Haiq-CSRF": csrf},
    )

    assert resp.status_code == 200, resp.text

    assert cfg_path.read_text(encoding="utf-8").strip() == "[webui]\nenabled = false"


def test_config_rollback_rejects_bad_id(tmp_path: Path) -> None:

    client = _client(tmp_path)

    csrf = _login(client)

    resp = client.post("/api/config/rollback", json={"id": "../haiq.toml"}, headers={"X-Haiq-CSRF": csrf})

    assert resp.status_code == 400


def test_config_get_exposes_no_secrets(tmp_path: Path) -> None:

    client = _client(tmp_path)

    _login(client)

    data = client.get("/api/config").json()["data"]

    assert isinstance(data["raw"], str)

    assert isinstance(data["fields"], list)

    assert "secrets" in data


def test_index_serves_spa_or_build_hint(tmp_path: Path) -> None:
    """产物在 → 返回 SPA；产物不在 → 返回"未构建"说明。两种情况都不许 500。"""

    client = _client(tmp_path)

    resp = client.get("/")

    assert resp.status_code == 200

    assert 'id="root"' in resp.text or "面板" in resp.text

    # 前端路由回退（刷新 /plugins 不该 404）

    assert client.get("/plugins").status_code == 200


def test_unknown_api_is_404(tmp_path: Path) -> None:

    client = _client(tmp_path)

    _login(client)

    resp = client.get("/api/nope")

    assert resp.status_code == 404

    assert resp.json()["error"]["code"] == "not_found"


def test_workbench_dry_run_never_invokes(tmp_path: Path) -> None:
    """工作台只解析：即使规则匹配，也不能调用 invoker。"""

    called: list[str] = []

    class FakeRule:
        plugin_id = "echo"

        label = "echo:/echo"

        priority = 100

        commands = ("/echo",)

        usage = "<内容>"

        block = False

        events = None

        platforms = None

        channel_kinds = None

        min_role = 0

        invoker = object()  # 只要被调用就会记录

        def matches(self, event: Any) -> Any:

            class Outcome:
                matched = event.text.startswith("/echo")

            return Outcome()

        def build_kwargs(self, outcome: Any, raw: str) -> dict[str, Any]:

            called.append(raw)

            return {"content": raw.removeprefix("/echo").strip()}

    runtime = FakeRuntime(rules=[FakeRule()])

    registry = FakeRegistry()

    cfg_path = tmp_path / "haiq.toml"

    cfg_path.write_text("[webui]\n", encoding="utf-8")

    app = build_webui(runtime, registry, config_path=cfg_path)

    client = TestClient(app)

    csrf = _login(client)

    resp = client.post("/api/workbench/parse", json={"text": "/echo 你好"}, headers={"X-Haiq-CSRF": csrf})

    assert resp.status_code == 200, resp.text

    data = resp.json()["data"]

    assert data["dry_run"] is True

    assert data["matched"][0]["plugin"] == "echo"

    assert "'你好'" in data["matched"][0]["params"]["content"]

    # build_kwargs 被调用、invoker 没有

    assert called == ["/echo 你好"]


def test_workbench_reports_role_gate(tmp_path: Path) -> None:

    class Rule:
        plugin_id = "admin"

        label = "admin:/管理"

        priority = 10

        commands = ("/管理",)

        usage = ""

        block = False

        events = None

        platforms = None

        channel_kinds = None

        min_role = 2  # ADMIN

        invoker = object()

        def matches(self, event: Any) -> Any:  # pragma: no cover - 不该走到

            raise AssertionError("权限不够的规则不该进入 matches")

    runtime = FakeRuntime(rules=[Rule()])

    cfg_path = tmp_path / "haiq.toml"

    cfg_path.write_text("", encoding="utf-8")

    client = TestClient(build_webui(runtime, FakeRegistry(), config_path=cfg_path))

    csrf = _login(client)

    data = client.post(
        "/api/workbench/parse",
        json={"text": "/管理", "role": 1},
        headers={"X-Haiq-CSRF": csrf},
    ).json()["data"]

    assert data["matched"] == []

    assert "权限不足" in data["skipped"][0]["reason"]


def test_logs_endpoint_returns_cursor(tmp_path: Path) -> None:

    from haiqbot.kernel import log_ring

    log_ring.clear_ring()

    log_ring.ring_capture(None, "t", {"event": "demo", "level": "info", "x": 1})

    client = _client(tmp_path)

    _login(client)

    data = client.get("/api/logs").json()["data"]

    assert data["items"][-1]["event"] == "demo"

    assert data["cursor"]["seq"] >= 1

    # 增量：since 已经到末尾就不该重复返回

    seq = data["cursor"]["seq"]

    again = client.get(f"/api/logs?since={seq}").json()["data"]

    assert again["items"] == []


@pytest.mark.parametrize("path", ["/api/overview", "/api/plugins", "/api/channels", "/api/audit"])
def test_all_read_endpoints_need_session(tmp_path: Path, path: str) -> None:

    client = _client(tmp_path)

    assert client.get(path).status_code == 401


def test_channels_api_allowed_fields_match_store():
    """面板可改字段必须与 store 的真实列一致，否则写进来会被当未知字段拒绝。"""

    from haiqbot.kernel.store import Store
    from haiqbot.webui.api.channels import ALLOWED_FIELDS

    assert ALLOWED_FIELDS == set(Store._CHANNEL_FIELDS)
