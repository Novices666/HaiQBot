"""控制面：令牌认证、字段白名单、热更新、持久化与审计。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest

from haiqbot.config import WebuiCfg, dump_default_config
from haiqbot.kernel import config_writer
from haiqbot.kernel.config_schema import SCHEMA
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
from haiqbot.services.media_store import MediaStore
from haiqbot.webui import build_webui

TOKEN = "ut-token-0123456789abcdef"
FAKE_API_KEY = "sk-ut-fake-key-do-not-leak-9000"


# -- 配置写回器 ------------------------------------------------------------- #


def test_writer_replaces_value_in_the_right_section() -> None:
    text = '[chat]\nmodel = "a"  # 注释要留着\n\n[image]\nmodel = "b"\n'
    out = config_writer.set_value(text, "chat", "model", "c")
    assert 'model = "c"  # 注释要留着' in out
    assert '[image]\nmodel = "b"' in out, "不能误改其它 section 的同名 key"


def test_writer_inserts_missing_key_into_existing_section() -> None:
    text = '[chat]\nmodel = "a"\n\n[image]\nmodel = "b"\n'
    out = config_writer.set_value(text, "chat", "retries", 5)
    parsed = tomllib.loads(out)
    assert parsed["chat"]["retries"] == 5
    assert parsed["image"]["model"] == "b"


def test_writer_creates_missing_section() -> None:
    out = config_writer.set_value('[chat]\nmodel = "a"\n', "quota", "enabled", False)
    assert tomllib.loads(out)["quota"]["enabled"] is False


def test_writer_renders_arrays_and_escapes_strings() -> None:
    out = config_writer.set_many(
        "", {"plugins.enabled": ["echo", "help"], "chat.system_prompt": '说"你好"\\n'}
    )
    parsed = tomllib.loads(out)
    assert parsed["plugins"]["enabled"] == ["echo", "help"]
    assert parsed["chat"]["system_prompt"] == '说"你好"\\n'


def test_default_template_is_valid_toml_and_accepts_patches() -> None:
    """模板本身必须是合法 TOML —— 改模板时最容易把 section 头弄丢。"""
    parsed = tomllib.loads(dump_default_config())
    for section in ("media", "webui", "onebot", "official", "quota", "policy", "plugins"):
        assert section in parsed, f"模板缺少 [{section}]"
    # 生图 / 对话的参数已归插件所有
    for pid in ("painter", "chat"):
        assert pid in parsed["plugins"], f"模板缺少 [plugins.{pid}]"

    patched = config_writer.set_many(
        dump_default_config(), {"plugins.chat.model": "m2", "plugins.enabled": ["echo"]}
    )
    reread = tomllib.loads(patched)
    assert reread["plugins"]["chat"]["model"] == "m2"
    assert reread["plugins"]["enabled"] == ["echo"]
    assert reread["plugins"]["painter"]["model"], "其它 section 不应被破坏"


# -- 夹具 ------------------------------------------------------------------- #


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "haiq.toml"
    path.write_text(dump_default_config(), encoding="utf-8")
    return path


@pytest.fixture
def ctl(runtime: Runtime, vault: object, config_file: Path) -> Runtime:
    runtime.config.webui = WebuiCfg(enabled=False, write_enabled=True, token_ref="WEFT_WEBUI_TOKEN")
    runtime.config.config_path = config_file
    vault.set("WEFT_WEBUI_TOKEN", TOKEN)  # type: ignore[attr-defined]
    return runtime


@pytest.fixture
async def registry(ctl: Runtime) -> Registry:
    reg = Registry(ctl, storage_root=Path(ctl.config.data_dir) / "plugins")
    await reg.load("help")
    return reg


@pytest.fixture
async def ui(ctl: Runtime, registry: Registry) -> object:
    ctl.media = MediaStore(ctl.store, ctl.config.media, webui=ctl.config.webui)
    return build_webui(ctl, registry)


_CSRF = {"value": ""}


class _LoggedClient(httpx.AsyncClient):
    """进 `async with` 时自动登录，把 CSRF 记到模块级。

    这样旧的 `headers=_auth()` 调用点一行都不用改 —— 换的是"怎么进门"，
    不是"断言什么"。
    """

    async def __aenter__(self) -> _LoggedClient:
        client = await super().__aenter__()
        resp = await client.post("/api/auth/login", json={"token": TOKEN})
        _CSRF["value"] = resp.json().get("data", {}).get("csrf", "") if resp.status_code == 200 else ""
        return client


async def _client(app: object) -> httpx.AsyncClient:
    return _LoggedClient(transport=httpx.ASGITransport(app=app), base_url="http://haiqbot.local")


def _auth(token: str | None = TOKEN) -> dict[str, str]:
    """新模型下写操作要的是 CSRF 头，不再是 Authorization。"""
    return {"X-Haiq-CSRF": _CSRF["value"]} if token else {}


# -- 认证 ------------------------------------------------------------------- #


async def test_write_without_csrf_is_403(ui: object) -> None:
    """已登录但没带 CSRF 头 → 拒绝（防跨站写）。"""
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"chat.model": "x"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf"


async def test_write_with_wrong_csrf_is_403(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"chat.model": "x"}, headers={"X-Haiq-CSRF": "nope"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf"


async def test_write_is_rejected_when_vault_has_no_token(ui: object, ctl: Runtime) -> None:
    """库里没有令牌 → 登录拿不到会话，写操作一律 401。"""
    ctl.vault.delete("WEFT_WEBUI_TOKEN")
    async with await _client(ui) as client:
        wait = await client.post("/api/config", json={"chat.model": "x"}, headers=_auth())
        login = await client.post("/api/auth/login", json={"token": TOKEN})
    assert wait.status_code == 401
    assert login.status_code == 503
    assert login.json()["error"]["code"] == "token_missing"


async def test_write_is_403_when_panel_writes_disabled(ui: object, ctl: Runtime) -> None:
    ctl.config.webui.write_enabled = False
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"chat.model": "x"}, headers=_auth())
    assert resp.status_code == 403


async def test_reads_need_session(ui: object) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=ui), base_url="http://haiqbot.local"
    ) as anon:
        assert (await anon.get("/api/config")).status_code == 401
        assert (await anon.get("/api/plugins")).status_code == 401
        assert (await anon.get("/api/overview")).status_code == 401

    async with await _client(ui) as client:
        assert (await client.get("/api/config")).status_code == 200
        assert (await client.get("/api/plugins")).status_code == 200
        assert (await client.get("/api/overview")).status_code == 200


# -- 密钥不外带 ------------------------------------------------------------- #


async def test_config_get_exposes_no_secret_values(ui: object, ctl: Runtime) -> None:
    ctl.config.plugin_config.setdefault("painter", {})["api_key_ref"] = "WEFT_IMAGE_API_KEY"
    ctl.vault.set("WEFT_IMAGE_API_KEY", FAKE_API_KEY)
    async with await _client(ui) as client:
        resp = await client.get("/api/config")

    assert resp.status_code == 200
    assert FAKE_API_KEY not in resp.text, "密钥真值绝不能出现在面板接口里"
    body = resp.json()["data"]
    painter_secret = next(s for s in body["secrets"] if s["path"] == "plugin:painter.api_key_ref")
    assert painter_secret["set"] is True
    assert painter_secret["ref"] == "WEFT_IMAGE_API_KEY"


async def test_config_get_lists_every_editable_field(ui: object) -> None:
    async with await _client(ui) as client:
        body = (await client.get("/api/config")).json()["data"]
    assert {f["path"] for f in body["fields"]} == {spec.path for spec in SCHEMA}


# -- 字段白名单 ------------------------------------------------------------- #


@pytest.mark.parametrize(
    "payload",
    [
        {"nonexistent.key": 1},
        {"path.secrets_dir": "/tmp/evil"},
        {"path.db_path": "/tmp/evil.db"},
        {"path.data_dir": "/tmp/x"},
        {"webui.port": 9999},
        {"media.dir": "/tmp/media"},
    ],
)
async def test_non_whitelisted_fields_are_rejected(
    ui: object, ctl: Runtime, config_file: Path, payload: dict
) -> None:
    before = config_file.read_text(encoding="utf-8")
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json=payload, headers=_auth())
    assert resp.status_code == 403
    assert config_file.read_text(encoding="utf-8") == before, "被拒绝的请求不得落盘"


async def test_invalid_values_are_rejected(ui: object, ctl: Runtime, config_file: Path) -> None:
    before = config_file.read_text(encoding="utf-8")
    async with await _client(ui) as client:
        too_big = await client.post("/api/config", json={"quota.per_user_per_day": 999999}, headers=_auth())
        not_a_number = await client.post("/api/config", json={"onebot.port": "abc"}, headers=_auth())
        empty = await client.post("/api/config", json={}, headers=_auth())
    assert too_big.status_code == 400
    assert not_a_number.status_code == 400
    assert empty.status_code == 400
    assert config_file.read_text(encoding="utf-8") == before


# -- 热更新与持久化 --------------------------------------------------------- #


async def test_config_post_applies_and_persists(ui: object, ctl: Runtime, config_file: Path) -> None:
    async with await _client(ui) as client:
        resp = await client.post(
            "/api/config",
            json={"quota.per_user_per_day": 33, "quota.enabled": False},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert ctl.config.quota.per_user_per_day == 33
    assert ctl.config.quota.enabled is False
    assert ctl.quota.cfg.enabled is False, "配额服务持的是同一份子模型，必须跟着变"

    disk = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert disk["quota"]["per_user_per_day"] == 33
    assert disk["quota"]["enabled"] is False
    # 插件段不能被顺手改掉
    assert (
        disk["plugins"]["chat"]["model"] == tomllib.loads(dump_default_config())["plugins"]["chat"]["model"]
    )


async def test_config_post_updates_shared_submodel(ui: object, ctl: Runtime) -> None:
    """媒体服务持的是同一份子模型对象 —— 改配置必须立刻在服务里生效。"""
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"media.ttl_seconds": 3600}, headers=_auth())

    assert resp.status_code == 200
    assert ctl.config.media.ttl_seconds == 3600
    assert ctl.media.cfg.ttl_seconds == 3600


async def test_unchanged_values_report_no_write(ui: object, ctl: Runtime) -> None:
    async with await _client(ui) as client:
        resp = await client.post(
            "/api/config", json={"quota.per_user_per_day": ctl.config.quota.per_user_per_day}, headers=_auth()
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["changed"] == []
    assert resp.json()["data"]["warnings"]


# -- 插件启停 --------------------------------------------------------------- #


async def test_plugin_disable_then_enable(
    ui: object, ctl: Runtime, registry: Registry, config_file: Path
) -> None:
    async with await _client(ui) as client:
        off = await client.post("/api/plugins/help/disable", headers=_auth())

    assert off.status_code == 200
    assert off.json()["data"]["enabled"] is False
    assert "help" not in registry.handles()
    assert "help" not in ctl.config.plugins_enabled
    assert "help" not in tomllib.loads(config_file.read_text(encoding="utf-8"))["plugins"]["enabled"]

    async with await _client(ui) as client:
        on = await client.post("/api/plugins/help/enable", headers=_auth())

    assert on.status_code == 200
    assert on.json()["data"]["loaded"] is True
    assert "help" in registry.handles()
    assert "help" in ctl.config.plugins_enabled
    assert "help" in tomllib.loads(config_file.read_text(encoding="utf-8"))["plugins"]["enabled"]


async def test_plugin_unknown_id_is_404(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/nope/enable", headers=_auth())
    assert resp.status_code == 404


async def test_reload_requires_loaded_plugin(ui: object, ctl: Runtime) -> None:
    async with await _client(ui) as client:
        ok = await client.post("/api/plugins/help/reload", headers=_auth())
        # echo 在 plugins_enabled 里但从没 load 过
        not_loaded = await client.post("/api/plugins/echo/reload", headers=_auth())

    assert ok.status_code == 200
    assert ok.json()["data"]["reloaded"] is True
    assert not_loaded.status_code == 409


async def test_plugin_write_also_needs_csrf(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/help/disable")
    assert resp.status_code == 403


# -- 审计 ------------------------------------------------------------------- #


async def test_control_actions_are_audited(ui: object, ctl: Runtime) -> None:
    async with await _client(ui) as client:
        await client.post("/api/config", json={"quota.per_user_per_day": 999}, headers=_auth())
        await client.post("/api/plugins/help/disable", headers=_auth())
        await client.post("/api/plugins/help/enable", headers=_auth())
        await client.post("/api/plugins/help/reload", headers=_auth())

    rows = await ctl.store.recent_audit(50)
    actions = [r["action"] for r in rows]
    assert "webui:config.update" in actions
    assert "webui:plugin.disable" in actions
    assert "webui:plugin.enable" in actions
    assert "webui:plugin.reload" in actions
    assert all(r["actor"] == "webui" for r in rows if r["action"].startswith("webui:"))
    # 审计里不能出现密钥
    assert FAKE_API_KEY not in str(rows)


# -- 凭据热读 --------------------------------------------------------------- #


def test_vault_sees_token_rotated_by_another_process(tmp_path: Path) -> None:
    """`haiq webui token --generate` 是独立进程；运行中的实例必须能看见新值。

    否则轮换令牌后面板一直 401，只能重启才能恢复 —— 这正是 DPAPI 后端
    进程内缓存曾经踩的坑。
    """
    import os

    from haiqbot.kernel.vault import DpapiFileBackend, PlainFileBackend, _dpapi_available

    path = tmp_path / "secrets" / "vault.bin"
    backend_cls = DpapiFileBackend if _dpapi_available() else PlainFileBackend
    live = backend_cls(path)  # 模拟「运行中的实例」
    live.set("WEFT_WEBUI_TOKEN", "first-token")

    other = backend_cls(path)  # 模拟「CLI 那个独立进程」
    other.set("WEFT_WEBUI_TOKEN", "second-token")
    os.utime(path, ns=(1, 1))  # 保证 mtime 一定变化

    assert live.get("WEFT_WEBUI_TOKEN") == "second-token"
    assert "WEFT_WEBUI_TOKEN" in live.keys()
