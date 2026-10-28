"""框架层收尾的行为测试：明文读取审计、doctor 退出码。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from haiqbot.config import HaiqConfig, OneBotCfg, VaultCfg, WebuiCfg
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


def _cfg_without_onebot_token(tmp_path: Path, *, mode: str) -> HaiqConfig:
    return HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        vault=VaultCfg(enc="none"),
        webui=WebuiCfg(enabled=False, write_enabled=False),
        onebot=OneBotCfg(enabled=True, mode=mode, access_token_ref="", host="0.0.0.0"),
    )


def test_doctor_does_not_call_an_empty_ref_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    """空 ref = 这一条不使用鉴权，不是「漏配」。

    协议端本来没设 token 时就会把 access_token_ref 留空；此时报「缺失（必需）」
    会让人去 vault 里找一个**不该存在**的键 —— 那是误导，不是发现。
    """
    cfg = _cfg_without_onebot_token(tmp_path, mode="client")
    vault = Vault.from_config(cfg)
    for ref in ("HAIQ_IMAGE_API_KEY", "HAIQ_CHAT_API_KEY"):
        vault.set(ref, "v")
    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)

    code = _doctor(None)
    out = capsys.readouterr().out

    assert "OneBot access_token：未配置（这一条不使用鉴权）" in out
    assert "OneBot access_token（" not in out, "空 ref 不该再报成缺失"
    assert code in (0, 1)


def test_doctor_warns_when_inbound_mode_has_no_token(tmp_path: Path, monkeypatch, capsys) -> None:
    """入站模式（server / http）不收 token = 任何人都能推事件进来。"""
    cfg = _cfg_without_onebot_token(tmp_path, mode="http")
    vault = Vault.from_config(cfg)
    for ref in ("HAIQ_IMAGE_API_KEY", "HAIQ_CHAT_API_KEY"):
        vault.set(ref, "v")
    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)

    code = _doctor(None)
    out = capsys.readouterr().out

    assert "任何人都能推事件" in out
    assert code == 1, "这条只算警告（不算错误）"


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


def test_doctor_warns_on_legacy_passphrase_file(tmp_path: Path, monkeypatch, capsys) -> None:
    """文件存口令这套已经下线：看到遗留文件就该喊「删掉」，而不是教用户继续用。"""
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
    assert "遗留的口令文件" in out
    assert ".vault-pass.txt" in out
    assert "rotate-vault-pass.ps1" not in out
    assert code >= 1


def test_doctor_warns_on_legacy_dpapi_file(tmp_path: Path, monkeypatch, capsys) -> None:
    """DPAPI 口令文件也不再被读：同样只提醒删掉。"""
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
    (secrets / ".vault-pass.dpapi").write_text("QUJD", encoding="utf-8")

    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)
    code = _doctor(str(cfg_path))
    out = capsys.readouterr().out
    assert ".vault-pass.dpapi" in out
    assert "遗留的口令文件" in out
    # 退出码受环境影响（宿主机开着代理就会多一条警告），只能松着断言
    assert code in (0, 1)


def test_doctor_reports_passphrase_source(tmp_path: Path, monkeypatch, capsys) -> None:
    """aead 时 doctor 要说清口令从哪来 —— 环境变量在就有，不在就警告。"""
    cfg_path = tmp_path / "haiq.toml"
    cfg_path.write_text("", encoding="utf-8")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    cfg = HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=secrets,
        vault=VaultCfg(enc="aead"),
        webui=WebuiCfg(enabled=False, write_enabled=False),
    )
    monkeypatch.setattr("haiqbot.cli.load_config", lambda _p: cfg)

    monkeypatch.setenv("HAIQ_VAULT_PASSPHRASE", "a-passphrase")
    _doctor(str(cfg_path))
    out = capsys.readouterr().out
    assert "口令来源：环境变量 HAIQ_VAULT_PASSPHRASE OK" in out

    monkeypatch.delenv("HAIQ_VAULT_PASSPHRASE", raising=False)
    _doctor(str(cfg_path))
    out = capsys.readouterr().out
    assert "没拿到 vault 主口令" in out
    assert ".env" in out
