"""控制面第二批：插件设置页、连接健康度、配置下拉候选。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest

from haiqbot.config import HaiqConfig, MediaCfg, OneBotCfg, PolicyCfg, QuotaCfg, WebuiCfg, dump_default_config
from haiqbot.kernel.bus import Bus
from haiqbot.kernel.governance import AuditLog, Idempotency, QuotaService, RoleResolver
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
from haiqbot.services.media_store import MediaStore
from haiqbot.webui import build_webui

TOKEN = "ut-token-0123456789abcdef"


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "haiq.toml"
    path.write_text(dump_default_config(), encoding="utf-8")
    return path


@pytest.fixture
def ctl(runtime: Runtime, vault: object, config_file: Path) -> Runtime:
    runtime.config.webui = WebuiCfg(enabled=False, write_enabled=True, token_ref="HAIQ_WEBUI_TOKEN")
    runtime.config.config_path = config_file
    vault.set("HAIQ_WEBUI_TOKEN", TOKEN)  # type: ignore[attr-defined]
    return runtime


@pytest.fixture
async def registry(ctl: Runtime) -> Registry:
    return Registry(ctl, storage_root=Path(ctl.config.data_dir) / "plugins")


@pytest.fixture
async def ui(ctl: Runtime, registry: Registry) -> object:
    ctl.media = MediaStore(ctl.store, ctl.config.media, webui=ctl.config.webui)
    return build_webui(ctl, registry)


_CSRF = {"value": ""}


class _LoggedClient(httpx.AsyncClient):
    async def __aenter__(self) -> _LoggedClient:
        client = await super().__aenter__()
        resp = await client.post("/api/auth/login", json={"token": TOKEN})
        _CSRF["value"] = resp.json().get("data", {}).get("csrf", "") if resp.status_code == 200 else ""
        return client


async def _client(app: object) -> httpx.AsyncClient:
    return _LoggedClient(transport=httpx.ASGITransport(app=app), base_url="http://haiqbot.local")


def _auth(token: str | None = TOKEN) -> dict[str, str]:
    """新模型：写操作要 CSRF 头。"""
    return {"X-Haiq-CSRF": _CSRF["value"]} if token else {}


# -- 插件设置页 ------------------------------------------------------------- #


async def test_plugins_endpoint_exposes_config_and_schema(ui: object) -> None:
    async with await _client(ui) as client:
        rows = (await client.get("/api/plugins")).json()["data"]
    chat = next(r for r in rows if r["id"] == "assistant")
    keys = [f["key"] for f in chat["config_schema"]]
    assert keys, "assistant 应声明自己的参数（image_* / chat_* …）"
    assert "chat_temperature" in keys
    assert "image_provider" in keys, "提供方选择权归插件所有"
    assert isinstance(chat["config"], dict)


async def test_plugin_config_requires_csrf(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/assistant/config", json={"chat_temperature": 0.3})
    assert resp.status_code == 403


async def test_plugin_config_persists_and_hot_applies(
    ui: object, ctl: Runtime, registry: Registry, config_file: Path
) -> None:
    await registry.load("assistant")  # 要 loaded 才能验证热更新
    handle = registry.handles()["assistant"]

    async with await _client(ui) as client:
        resp = await client.post(
            "/api/plugins/assistant/config", json={"chat_temperature": 0.3}, headers=_auth()
        )

    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["applied_live"] is True
    assert body["changed"][0]["key"] == "chat_temperature"

    # 1) 落盘：主配置只剩注册，参数进了 data/plugins/<id>.toml
    main = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert set(main["plugins"]) == {"enabled"}, "插件参数不该再写回主配置"
    from haiqbot.config import plugin_config_file

    plugin_file = plugin_config_file(ctl.config.data_dir, "assistant")
    assert plugin_file.is_file(), "插件配置应落到 data/plugins/<id>.toml"
    assert tomllib.loads(plugin_file.read_text(encoding="utf-8"))["chat_temperature"] == 0.3
    # 2) 内存：runtime config + 运行中插件的 ctx.config 都要跟着变
    assert ctl.config.plugin_config["assistant"]["chat_temperature"] == 0.3
    assert handle.context.config["chat_temperature"] == 0.3
    assert handle.plugin.config["chat_temperature"] == 0.3


async def test_plugin_config_accepts_non_string_scalars(ui: object, ctl: Runtime) -> None:
    async with await _client(ui) as client:
        resp = await client.post(
            "/api/plugins/admin/config", json={"max_rows": 20, "verbose": True}, headers=_auth()
        )
    assert resp.status_code == 200
    cfg = ctl.config.plugin_config["admin"]
    assert cfg["max_rows"] == 20
    assert cfg["verbose"] is True


@pytest.mark.parametrize("key", ["bad key", "a-b", "", "x" * 80, "中文"])
async def test_plugin_config_rejects_bad_keys(ui: object, config_file: Path, key: str) -> None:
    before = config_file.read_text(encoding="utf-8")
    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/assistant/config", json={key: "v"}, headers=_auth())
    assert resp.status_code == 400
    assert config_file.read_text(encoding="utf-8") == before


async def test_plugin_config_unknown_plugin_is_404(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/nope/config", json={"a": 1}, headers=_auth())
    assert resp.status_code == 404


async def test_plugin_config_is_audited(ui: object, ctl: Runtime) -> None:
    async with await _client(ui) as client:
        await client.post("/api/plugins/assistant/config", json={"chat_temperature": 0.5}, headers=_auth())
    actions = [r["action"] for r in await ctl.store.recent_audit(20)]
    assert "webui:plugin.config" in actions


# -- 连接健康度 ------------------------------------------------------------- #


async def test_overview_reports_uptime_and_health(ui: object, ctl: Runtime) -> None:
    from haiqbot.adapters.mock import MockAdapter

    ctl.adapters = {"mock": MockAdapter()}
    ctl.note_event(handled=True)

    async with await _client(ui) as client:
        body = (await client.get("/api/overview")).json()["data"]

    assert body["runtime"]["uptime_seconds"] >= 0
    assert body["runtime"]["events_total"] == 1
    assert body["runtime"]["handled_total"] == 1
    assert body["runtime"]["last_event_ts"] is not None
    assert "mock" in body["platforms"]
    health = body["platforms"]["mock"]["health"]
    # mock 没有可观测的连接态 —— 必须是 None，不能硬编成 True
    assert health["connected"] is None
    assert "last_event_ts" in health


def test_onebot_health_reports_disconnected_before_start() -> None:
    from haiqbot.adapters.onebot11 import OneBotAdapter

    adapter = OneBotAdapter(OneBotCfg(enabled=True), token_provider=lambda: None)
    h = adapter.health()
    assert h["connected"] is False
    assert h["reconnects"] == 0
    assert h["mode"] == "client"
    assert h["target"] == adapter.cfg.url
    assert h["last_error"] is None


def test_runtime_health_marks_degraded_when_platform_down() -> None:
    from haiqbot.adapters.onebot11 import OneBotAdapter

    cfg = HaiqConfig(policy=PolicyCfg(), quota=QuotaCfg(), media=MediaCfg())
    resolver = RoleResolver.from_policy(cfg.policy)
    rt = Runtime(
        config=cfg,
        store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        bus=Bus(),
        resolver=resolver,
        quota=QuotaService(None, cfg.quota, resolver),  # type: ignore[arg-type]
        idempotency=Idempotency(None),  # type: ignore[arg-type]
        audit=AuditLog(None),  # type: ignore[arg-type]
    )
    rt.adapters = {"onebot": OneBotAdapter(OneBotCfg(), token_provider=lambda: None)}
    assert rt.health()["ok"] is False, "明确掉线的平台必须让整体判为不健康"


# -- 配置下拉候选 ----------------------------------------------------------- #


async def test_config_fields_carry_choices(ui: object) -> None:
    async with await _client(ui) as client:
        body = (await client.get("/api/config")).json()["data"]
    by_path = {f["path"]: f for f in body["fields"]}
    assert by_path["onebot.image_transport"]["choices"], "投递方式应给出候选"
    assert "base64" in by_path["onebot.image_transport"]["choices"]
    assert by_path["log.level"]["choices"]
    # 纯文本字段不该有候选，否则下拉里塞不进用户想写的值
    assert by_path["official.api_base"]["choices"] == []
