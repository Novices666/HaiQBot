"""框架层收尾的行为测试：明文读取审计、doctor 退出码。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from haiqbot.config import HaiqConfig, VaultCfg, WebuiCfg
from haiqbot.kernel.plugin import SecretHandle, SecretSlot
from haiqbot.kernel.vault import PlainFileBackend, Vault


class _FakeAudit:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def test_plain_read_is_audited_first_and_every_100(tmp_path: Path) -> None:
    audit = _FakeAudit()
    vault = Vault([PlainFileBackend(tmp_path / "v.json")])
    vault.set("K", "secret-value")
    handle = SecretHandle("painter", "api_key", SecretSlot(label="L", ref="K"), {}, vault, audit=audit)

    for _ in range(250):
        handle.text()

    await asyncio.sleep(0.01)  # 让 create_task 的审计协程跑完
    # reads = 1 / 100 / 200  → 3 条
    assert len(audit.calls) == 3
    assert {c["action"] for c in audit.calls} == {"secret_plain_read"}
    assert {c["plugin_id"] for c in audit.calls} == {"painter"}
    # 审计里不能有明文
    for call in audit.calls:
        assert "secret-value" not in call["detail"]


async def test_plain_read_audit_is_a_noop_without_audit() -> None:
    handle = SecretHandle("p", "api_key", SecretSlot(label="L", ref="K"), {}, None)
    assert handle.text() is None


def _doctor(ns_config: str | None) -> int:
    from haiqbot.cli import _cmd_doctor

    return _cmd_doctor(argparse.Namespace(config=ns_config))


def test_doctor_returns_2_when_required_secret_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none"),
        webui=WebuiCfg(enabled=False, write_enabled=False),
    )
    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)
    code = _doctor(None)
    out = capsys.readouterr().out
    assert "assistant.image_api_key" in out
    assert code == 2


def test_doctor_returns_0_or_1_when_secrets_present(tmp_path: Path, monkeypatch, capsys) -> None:
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none"),
        webui=WebuiCfg(enabled=False, write_enabled=False),
    )
    vault = Vault.from_config(cfg)
    for ref in ("HAIQ_IMAGE_API_KEY", "HAIQ_CHAT_API_KEY"):
        vault.set(ref, "v")
    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)
    code = _doctor(None)
    out = capsys.readouterr().out
    assert "结果" in out
    # 没错误；可能有系统代理警告，所以 0 或 1
    assert code in (0, 1)


def test_demo_segment_rendering_is_readable() -> None:
    """演示输出不能把段 dict 原样倒出来 —— 新用户第一眼看到的就是它。"""
    from haiqbot.app import App

    rendered = App._describe_segments(
        [
            {"kind": "text", "data": {"text": "你好"}},
            {"kind": "image", "data": {"media_id": "abcdef123456", "url": "http://x"}},
            {"kind": "file", "data": {"name": "报表.pdf"}},
            {"kind": "forward", "data": {}},
        ]
    )
    assert rendered == "你好 [图片 abcdef12] [文件 报表.pdf] [合并转发]"
    assert App._describe_segments([]) == ""


def test_doctor_warns_on_plaintext_passphrase_file(tmp_path: Path, monkeypatch, capsys) -> None:
    """vault 旁边躺着明文密码文件 = 加密白做，doctor 必须喊出来。"""
    cfg_path = tmp_path / "haiq.toml"
    cfg_path.write_text("", encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=secrets,
        vault=VaultCfg(enc="none"),
        webui=WebuiCfg(enabled=False, write_enabled=False),
    )
    vault = Vault.from_config(cfg)
    for ref in ("HAIQ_IMAGE_API_KEY", "HAIQ_CHAT_API_KEY", "HAIQ_ONEBOT_ACCESS_TOKEN"):
        vault.set(ref, "v")
    (secrets / ".vault-pass.txt").write_text("x" * 43, encoding="utf-8")

    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)
    code = _doctor(str(cfg_path))
    out = capsys.readouterr().out
    assert "明文密码文件还在" in out
    assert "rotate-vault-pass.ps1" in out
    assert code >= 1
