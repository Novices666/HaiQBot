"""CLI 两个真机 bug 的回归：

1. `haiq plugin enable`：实例在跑时永远回 `unauthorized` ——
   它发的是 `Authorization: Bearer`，而写接口只认「会话 Cookie + CSRF」；
   而且路径写成了 `/api/plugin/...`（少了 s），就算过了鉴权也是 404。
   更糟的是"收到 401"不算"连不上"，于是**不会**回落到离线路径 → CLI 完全不可用。
2. `haiq doctor`：容器里注入 `HTTP_PROXY=http://127.0.0.1:10808` 时，
   127.0.0.1 指的是容器自己 → 所有出站必失败（对话/生图全挂、日志刷 llm_retry）。
   这个必须报成**错误**并说清修法，不能只是"检测到系统代理"的警告。
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from haiqbot import cli
from haiqbot.config import HaiqConfig, VaultCfg, WebuiCfg
from haiqbot.kernel.vault import Vault


class _Resp:
    def __init__(self, payload: dict, *, headers: dict | None = None, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = headers or {}
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _Opener:
    """记录所有请求，按顺序返回预置响应。"""

    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = list(responses)
        self.requests: list[urllib.request.Request] = []

    def open(self, req: urllib.request.Request, timeout: float | None = None) -> _Resp:
        self.requests.append(req)
        return self._responses.pop(0)


# --------------------------------------------------------------------------- #
# _remote_control：登录换会话 + 带 CSRF 发写请求
# --------------------------------------------------------------------------- #
def _cfg_with_token(tmp_path: Path, vault: Vault) -> HaiqConfig:
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none"),
        webui=WebuiCfg(enabled=False, write_enabled=True, token_ref="HAIQ_WEBUI_TOKEN"),
    )
    vault.set("HAIQ_WEBUI_TOKEN", "ut-token-0123456789")
    return cfg


def test_remote_control_logs_in_then_sends_csrf(
    tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg_with_token(tmp_path, vault)
    monkeypatch.setattr(Vault, "from_config", classmethod(lambda cls, _cfg: vault))

    opener = _Opener(
        [
            _Resp(
                {"ok": True, "data": {"csrf": "csrf-abc"}},
                headers={"set-cookie": "haiq_session=v1.x.y; Path=/; HttpOnly"},
            ),
            _Resp({"ok": True, "data": {"id": "echo", "note": "已启用并加载"}}),
        ]
    )
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: opener)

    result = cli._remote_control(cfg, "/api/plugins/echo/enable")

    assert result is not None
    status, payload = result
    assert status == 200
    assert payload["data"]["note"] == "已启用并加载"

    login, write = opener.requests
    assert login.full_url.endswith("/api/auth/login")
    assert json.loads(login.data or b"{}")["token"] == "ut-token-0123456789"
    assert write.full_url.endswith("/api/plugins/echo/enable")
    assert write.get_header("X-haiq-csrf") == "csrf-abc"
    assert write.get_header("Cookie") == "haiq_session=v1.x.y"


def test_remote_control_returns_none_when_no_instance(
    tmp_path: Path, vault: Vault, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg_with_token(tmp_path, vault)
    monkeypatch.setattr(Vault, "from_config", classmethod(lambda cls, _cfg: vault))

    class _Dead:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: _Dead())
    assert cli._remote_control(cfg, "/api/plugins/echo/enable") is None


def test_plugin_action_path_is_plural_and_falls_back_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """401 不该让 CLI 死掉：退回离线写配置，并说明「重启才生效」。"""
    seen: list[str] = []

    def fake_remote(_cfg, path, body=None):
        seen.append(path)
        return 401, {"error": "未登录或会话已过期"}

    offline: list[bool] = []
    monkeypatch.setattr(cli, "_remote_control", fake_remote)
    monkeypatch.setattr(cli, "_plugin_offline", lambda _cfg, _args: offline.append(True) or 0)
    monkeypatch.setattr(cli, "load_config", lambda _p: HaiqConfig())
    monkeypatch.setattr(cli, "configure_logging", lambda *_a, **_k: None)

    code = cli._cmd_plugin(argparse.Namespace(action="enable", id="echo", config=None, json=False))
    out = capsys.readouterr().out

    assert code == 0
    assert seen == ["/api/plugins/echo/enable"]
    assert offline == [True]
    assert "重启" in out


# --------------------------------------------------------------------------- #
# doctor：容器里的 127.0.0.1 代理必须报错
# --------------------------------------------------------------------------- #
def _doctor_cfg(tmp_path: Path) -> HaiqConfig:
    return HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none"),
        webui=WebuiCfg(enabled=False, write_enabled=False),
    )


def _run_doctor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, *, in_container: bool):
    cfg = _doctor_cfg(tmp_path)
    from haiqbot.kernel.vault import Vault as V

    for ref in ("HAIQ_IMAGE_API_KEY", "HAIQ_CHAT_API_KEY"):
        V.from_config(cfg).set(ref, "v")
    monkeypatch.setattr(cli, "load_config", lambda _p: cfg)
    monkeypatch.setattr(cli, "_in_container", lambda: in_container)
    monkeypatch.setattr(
        urllib.request,
        "getproxies",
        lambda: {"http": "http://127.0.0.1:10808", "https": "http://127.0.0.1:10808"},
    )
    code = cli._cmd_doctor(argparse.Namespace(config=None))
    return code, capsys.readouterr().out


def test_doctor_errors_on_loopback_proxy_inside_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run_doctor(tmp_path, monkeypatch, capsys, in_container=True)

    assert "容器内检测到指向本机的代理" in out
    assert "host.docker.internal" in out, "要给出确需代理时的写法"
    assert code == 2, "这不是警告，是必失败"


def test_doctor_treats_loopback_proxy_as_warning_on_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run_doctor(tmp_path, monkeypatch, capsys, in_container=False)

    assert "检测到系统代理" in out
    assert "容器内检测到指向本机的代理" not in out
    assert code == 1
