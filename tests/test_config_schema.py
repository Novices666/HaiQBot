"""字段元数据：单一来源、生效标记、只读约束、顶层键落盘。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import httpx
import pytest

from haiqbot.config import HaiqConfig, MediaCfg, PolicyCfg, QuotaCfg, WebuiCfg, dump_default_config
from haiqbot.kernel import control
from haiqbot.kernel.config_schema import BY_PATH, SCHEMA
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
from haiqbot.services.media_store import MediaStore
from haiqbot.webui import build_webui

TOKEN = "ut-token-0123456789abcdef"


def test_schema_covers_every_section() -> None:
    """配置类里出现的段都必须在 SCHEMA 里有字段，否则面板永远看不到它。"""
    sections = {
        name
        for name, value in HaiqConfig.model_fields.items()
        if hasattr(getattr(HaiqConfig, "model_fields", {}), "__getitem__")
        and value.annotation is not None
        and name in {"onebot", "official", "quota", "policy", "media", "webui"}
    }
    declared = {f.section for f in SCHEMA if f.section}
    assert sections <= declared, f"这些段没有字段定义：{sections - declared}"


def test_every_field_has_label_and_known_apply() -> None:
    for f in SCHEMA:
        assert f.label, f"{f.path} 缺中文标签"
        assert f.apply in {"hot", "plugin_reload", "reconnect", "restart"}, f.path
        assert f.group in {"access", "governance", "media", "kernel", "service"}, f.path


def test_toml_path_strips_pseudo_sections() -> None:
    assert BY_PATH["log.level"].toml_path == "log_level"
    assert BY_PATH["log.json"].toml_path == "log_json"
    assert BY_PATH["path.data_dir"].toml_path == "data_dir"
    # 插件私有参数（provider / model 等）不进内核 SCHEMA，由插件自己声明
    assert "chat.model" not in BY_PATH
    assert "image.provider" not in BY_PATH


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
async def ui(ctl: Runtime) -> object:
    ctl.media = MediaStore(ctl.store, ctl.config.media, webui=ctl.config.webui)
    registry = Registry(ctl, storage_root=Path(ctl.config.data_dir) / "plugins")
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


# -- 接口行为 --------------------------------------------------------------- #


async def test_describe_carries_apply_and_readonly(ui: object) -> None:
    async with await _client(ui) as client:
        body = (await client.get("/api/config")).json()["data"]
    by_path = {f["path"]: f for f in body["fields"]}

    assert by_path["onebot.url"]["apply"] == "reconnect"
    assert by_path["webui.port"]["readonly"] is True
    assert by_path["path.data_dir"]["readonly"] is True
    assert by_path["policy.owners"]["apply"] == "hot"
    assert by_path["onebot.enabled"]["apply"] == "restart"
    assert by_path["onebot.mode"]["choices"] == ["client", "server"]


async def test_readonly_field_is_rejected(ui: object, config_file: Path) -> None:
    before = config_file.read_text(encoding="utf-8")
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"webui.port": 9999}, headers=_auth())
    assert resp.status_code == 403
    assert "只读" in resp.json()["error"]["message"]
    assert config_file.read_text(encoding="utf-8") == before


async def test_top_level_field_writes_without_section(ui: object, ctl: Runtime, config_file: Path) -> None:
    """`log.level` 必须落成顶层 `log_level`，不能凭空造一个 `[log]` 段。"""
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"log.level": "DEBUG"}, headers=_auth())
    assert resp.status_code == 200
    assert ctl.config.log_level == "DEBUG"

    raw = config_file.read_text(encoding="utf-8")
    assert "[log]" not in raw, "不该造出 [log] 段"
    disk = tomllib.loads(raw)
    assert disk["log_level"] == "DEBUG"


async def test_list_field_accepts_string_and_list(ui: object, ctl: Runtime, config_file: Path) -> None:
    async with await _client(ui) as client:
        r1 = await client.post("/api/config", json={"policy.owners": "111, 222"}, headers=_auth())
        r2 = await client.post("/api/config", json={"policy.admins": ["333"]}, headers=_auth())
    assert r1.status_code == 200 and r2.status_code == 200
    assert ctl.config.policy.owners == ["111", "222"]
    assert ctl.config.policy.admins == ["333"]

    disk = tomllib.loads(config_file.read_text(encoding="utf-8"))
    assert disk["policy"]["owners"] == ["111", "222"]
    # resolver 是构造时拷走的 set，必须跟着刷新
    assert ctl.resolver.owners == {"111", "222"}


async def test_deferred_changes_are_reported_in_warnings(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"weui": 1}, headers=_auth())
        resp2 = await client.post("/api/config", json={"onebot.mode": "server"}, headers=_auth())
    assert resp.status_code == 403  # 未知字段
    assert resp2.status_code == 200
    assert any("需重连" in w for w in resp2.json()["data"]["warnings"])


async def test_enum_choice_is_validated(ui: object) -> None:
    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"onebot.mode": "p2p"}, headers=_auth())
    assert resp.status_code == 400
    assert "只能是" in resp.json()["error"]["message"]


def test_policy_defaults_are_lists() -> None:
    """列表字段的默认值必须是 list，否则前端 chips 渲染会崩。"""
    cfg = HaiqConfig(policy=PolicyCfg(), quota=QuotaCfg(), media=MediaCfg())
    assert isinstance(cfg.policy.owners, list)
    assert isinstance(cfg.policy.channel_whitelist, list)
    _ = control  # 保持模块可达，避免误删导入
