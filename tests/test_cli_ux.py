"""CLI 的「新人第一屏」行为：中文别乱码、缺口令别抛 traceback。"""

from __future__ import annotations

import sys
from pathlib import Path

from haiqbot import cli
from haiqbot.config import dump_default_config


class _FakeStream:
    """记录 reconfigure 的入参，但不真的改编码。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:  # noqa: ANN003
        self.calls.append(kwargs)

    def write(self, _text: str) -> int:
        return 0

    def flush(self) -> None:
        pass


def test_main_does_not_force_utf8_on_console(monkeypatch) -> None:
    """Windows 控制台默认 GBK：强制 stdout=UTF-8 会让所有中文变乱码。"""
    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    assert cli.main(["--version"]) == 0

    for stream in (out, err):
        assert stream.calls, "应当 reconfigure 一次（放宽 errors）"
        for kwargs in stream.calls:
            assert "encoding" not in kwargs, "不许改编码，否则 GBK 控制台全是乱码"
            assert kwargs.get("errors") == "replace"


def test_missing_passphrase_is_friendly_not_traceback(tmp_path: Path, monkeypatch, capsys) -> None:
    """`haiq webui token --generate` 在没有口令时只该看到一句人话、退出码 2。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "haiq.toml").write_text(dump_default_config(), encoding="utf-8")
    monkeypatch.delenv("HAIQ_VAULT_PASSPHRASE", raising=False)

    rc = cli.main(["webui", "token", "--generate"])

    out = capsys.readouterr().out
    assert rc == 2
    assert "凭据库打不开" in out
    assert "Traceback" not in out
