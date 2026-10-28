"""配置写不进去时，面板该说什么、该做什么。

真机报过这么一句：

    配置写入失败，已放弃停用：[Errno 13] Permission denied: 'haiq.toml'

两个问题都在这里钉住：
1. `haiq.toml` 是**相对**名字 —— 换目录启动（NSSM 服务的 CWD 是 System32、
   容器里按只读挂载）根本看不出在写哪个文件，所以 `config_path` 必须是绝对路径；
2. 一个只读属性把整块插件页变成不可用。启停是运行时开关，可以就地生效、
   只把「没落盘」这件事说清楚 —— 但绝不能说成「重启后依然生效」。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from haiqbot.config import WebuiCfg, dump_default_config, load_config
from haiqbot.kernel import control as control_mod
from haiqbot.kernel.registry import Registry
from haiqbot.kernel.runtime import Runtime
from haiqbot.services.media_store import MediaStore
from haiqbot.webui import build_webui

TOKEN = "ut-token-0123456789abcdef"
_CSRF = {"value": ""}


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


class _LoggedClient(httpx.AsyncClient):
    async def __aenter__(self) -> _LoggedClient:
        client = await super().__aenter__()
        resp = await client.post("/api/auth/login", json={"token": TOKEN})
        _CSRF["value"] = resp.json().get("data", {}).get("csrf", "") if resp.status_code == 200 else ""
        return client


async def _client(app: object) -> httpx.AsyncClient:
    return _LoggedClient(transport=httpx.ASGITransport(app=app), base_url="http://haiqbot.local")


def _auth() -> dict[str, str]:
    return {"X-Haiq-CSRF": _CSRF["value"]}


def _readonly(monkeypatch: pytest.MonkeyPatch) -> None:
    """让写配置必然失败（等价于只读挂载 / 文件被独占 / 无写权限）。"""

    def boom(path: object, changes: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(control_mod, "write_config", boom)


# -- 启停：降级但不撒谎 ----------------------------------------------------- #


async def test_disable_works_and_says_it_is_memory_only(
    ui: object, ctl: Runtime, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readonly(monkeypatch)
    await registry.load("echo")  # type: ignore[attr-defined]

    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/echo/disable", headers=_auth())

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["enabled"] is False
    assert data["persisted"] is False, "写不进去就必须如实说，不能当成成功糊过去"
    assert "echo" not in ctl.config.plugins_enabled
    assert "echo" not in registry.handles()  # type: ignore[attr-defined]
    assert "重启" in data["warning"], "要说明重启后会恢复"
    assert str(ctl.config.config_path) in data["warning"], "要指出到底在写哪个文件"


async def test_enable_also_degrades(
    ui: object, ctl: Runtime, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readonly(monkeypatch)
    ctl.config.plugins_enabled = [x for x in ctl.config.plugins_enabled if x != "help"]

    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/help/enable", headers=_auth())

    data = resp.json()["data"]
    assert data["enabled"] is True
    assert data["persisted"] is False
    assert "help" in ctl.config.plugins_enabled


async def test_normal_path_still_reports_persisted(ui: object, ctl: Runtime, registry: Registry) -> None:
    await registry.load("echo")  # type: ignore[attr-defined]
    async with await _client(ui) as client:
        resp = await client.post("/api/plugins/echo/disable", headers=_auth())
    data = resp.json()["data"]
    assert data["persisted"] is True
    assert "warning" not in data
    assert "echo" not in load_config(ctl.config.config_path).plugins_enabled


# -- 配置中心：写不进去就不改内存，并说清哪个文件 --------------------------- #


async def test_config_save_still_refuses_but_names_the_file(
    ui: object, ctl: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    _readonly(monkeypatch)
    before = ctl.config.quota.per_user_per_day

    async with await _client(ui) as client:
        resp = await client.post("/api/config", json={"quota.per_user_per_day": 7}, headers=_auth())

    assert resp.status_code == 500
    message = resp.json()["error"]["message"]
    assert "放弃本次修改" in message
    assert str(ctl.config.config_path) in message
    assert "Permission denied" in message
    # 配置中心不做「只改内存」的降级：文件没改成，内存也不许变
    assert ctl.config.quota.per_user_per_day == before


# -- 相对路径陷阱 ----------------------------------------------------------- #


def test_load_config_resolves_config_path_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对路径 + 换工作目录 = 往别的地方写，报错还看不出在哪。"""
    (tmp_path / "haiq.toml").write_text(dump_default_config(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = load_config("haiq.toml")

    assert cfg.config_path is not None
    assert cfg.config_path.is_absolute()
    assert cfg.config_path == (tmp_path / "haiq.toml").resolve()
