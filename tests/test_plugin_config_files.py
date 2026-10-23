"""插件配置外置：`data/plugins/<id>.toml` 是唯一落点，主 toml 只留注册。

这是「插件自己管配置」的落地形状 —— 内核不认识任何 key 的语义，只负责
「把这个文件读成 dict 交给插件」。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haiqbot.config import load_config, plugin_config_file


class _LogSink:
    """替掉模块级 log，只为断言告警事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def warning(self, event: str, **kw: object) -> None:
        self.events.append((event, kw))

    def info(self, *_a: object, **_kw: object) -> None:
        return None

    def error(self, *_a: object, **_kw: object) -> None:
        return None

    def exception(self, *_a: object, **_kw: object) -> None:
        return None


def _write_main(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "haiq.toml"
    # data_dir 用绝对路径：生产语义是「相对进程工作目录」，测试里 CWD 是仓库根，
    # 写相对路径会指到仓库的 data/ 去，反而测不到东西。
    data_dir = (tmp_path / "data").as_posix()
    cfg.write_text(
        f'data_dir = "{data_dir}"\n[plugins]\nenabled = ["demo"]\n\n{extra}',
        encoding="utf-8",
    )
    return cfg


def test_plugin_config_file_path(tmp_path: Path) -> None:
    assert plugin_config_file("data", "demo") == Path("data") / "plugins" / "demo.toml"
    assert plugin_config_file(tmp_path, "demo") == tmp_path / "plugins" / "demo.toml"


def test_reads_plugin_config_from_file(tmp_path: Path) -> None:
    _write_main(tmp_path)
    target = plugin_config_file(tmp_path / "data", "demo")
    target.parent.mkdir(parents=True)
    target.write_text('model = "from-file"\nretries = 3\n', encoding="utf-8")

    cfg = load_config(tmp_path / "haiq.toml")
    assert cfg.plugin_config["demo"] == {"model": "from-file", "retries": 3}


def test_file_wins_over_inline_section(tmp_path: Path) -> None:
    _write_main(tmp_path, '[plugins.demo]\nmodel = "from-inline"\n')
    target = plugin_config_file(tmp_path / "data", "demo")
    target.parent.mkdir(parents=True)
    target.write_text('model = "from-file"\n', encoding="utf-8")

    cfg = load_config(tmp_path / "haiq.toml")
    assert cfg.plugin_config["demo"]["model"] == "from-file", "文件是唯一主位置"


def test_inline_only_section_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """主 toml 里残留的 [plugins.<id>] 仍能用（兼容），但必须告警。"""
    from haiqbot import config as config_mod

    sink = _LogSink()
    monkeypatch.setattr(config_mod, "log", sink)

    _write_main(tmp_path, '[plugins.demo]\nmodel = "legacy"\n')
    cfg = load_config(tmp_path / "haiq.toml")

    assert cfg.plugin_config["demo"]["model"] == "legacy"
    assert any(
        event == "plugin_config_inline_deprecated" and kw.get("plugin") == "demo" for event, kw in sink.events
    ), sink.events


def test_missing_plugin_config_dir_is_fine(tmp_path: Path) -> None:
    """全新环境没有 data/plugins/ 也必须能启动。"""
    _write_main(tmp_path)
    cfg = load_config(tmp_path / "haiq.toml")
    assert cfg.plugin_config == {}


def test_broken_plugin_config_file_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一个坏文件不该让整台机器起不来。"""
    from haiqbot import config as config_mod

    sink = _LogSink()
    monkeypatch.setattr(config_mod, "log", sink)

    _write_main(tmp_path)
    target = plugin_config_file(tmp_path / "data", "demo")
    target.parent.mkdir(parents=True)
    target.write_text("这不是 toml [[[", encoding="utf-8")

    cfg = load_config(tmp_path / "haiq.toml")
    assert cfg.plugin_config == {}
    assert any(event == "plugin_config_unreadable" for event, _ in sink.events)
