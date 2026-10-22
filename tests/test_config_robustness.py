"""配置读取的健壮性：BOM、非 UTF-8、字段写错段位。

这三条都是实测踩出来的，不是假想：

1. PowerShell 5.1 的 `Set-Content -Encoding utf8` 会写出**带 BOM** 的文件 →
   `tomllib` 直接报 `Invalid statement (at line 1, column 1)`，用户完全看不出原因；
2. 记事本「另存为 ANSI」→ GBK 字节 → `UnicodeDecodeError` 同样裸抛；
3. `plugin_dirs = [...]` 写在任何 `[section]` 之后 → 变成段内键 → pydantic 静默忽略，
   整套本地目录插件体系无声失效。这一条最难自查，必须有告警。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haiqbot.config import _warn_misplaced_fields, load_config
from haiqbot.core.errors import ConfigError
from haiqbot.kernel import plugin_sources

BOM = b"\xef\xbb\xbf"


def _write_bom(path: Path, text: str) -> Path:
    path.write_bytes(BOM + text.encode("utf-8"))
    return path


def test_config_with_bom_loads(tmp_path: Path) -> None:
    """带 BOM 的 haiq.toml 必须能正常读（Windows 编辑器高频产物）。"""
    p = _write_bom(
        tmp_path / "haiq.toml",
        '[plugins]\nenabled = ["echo"]\n\n[media]\nttl_seconds = 120\n',
    )
    cfg = load_config(p)
    assert cfg.plugins_enabled == ["echo"]
    assert cfg.media.ttl_seconds == 120


def test_config_not_utf8_raises_config_error(tmp_path: Path) -> None:
    """非 UTF-8 编码要变成一句人话，而不是 UnicodeDecodeError 堆栈。"""
    p = tmp_path / "haiq.toml"
    p.write_bytes('[plugins]\nenabled = ["回声"]\n'.encode("gbk"))
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "UTF-8" in str(ei.value)


def test_config_syntax_error_raises_config_error(tmp_path: Path) -> None:
    """语法错也要可自救：提示里给出 `haiq init` 这条退路。"""
    p = tmp_path / "haiq.toml"
    p.write_text("data_dir = \n", encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(p)
    assert "haiq init" in str(ei.value)


def test_plugin_manifest_with_bom_is_discovered(tmp_path: Path) -> None:
    """plugin.toml 带 BOM 时，插件不能被静默丢弃。"""
    plug = tmp_path / "plugins" / "hello"
    plug.mkdir(parents=True)
    (plug / "__init__.py").write_text("", encoding="utf-8")
    _write_bom(plug / "plugin.toml", 'id = "hello"\nname = "打招呼"\nversion = "0.1.0"\n')

    cfg_path = tmp_path / "haiq.toml"
    dirs = str(tmp_path / "plugins").replace("\\", "/")
    cfg_path.write_text(f'plugin_dirs = ["{dirs}"]\n', encoding="utf-8")

    cfg = load_config(cfg_path)
    assert plugin_sources.local_plugin_dirs(cfg) == {"hello": plug}


def test_plugin_dirs_inside_section_is_reported(tmp_path: Path, monkeypatch) -> None:
    """写进别的段里必须告警（否则本地插件体系无声失效）。"""
    import haiqbot.kernel.log as logmod

    seen: list[tuple[str, dict]] = []

    class _FakeLog:
        def warning(self, event: str, **kwargs) -> None:
            seen.append((event, kwargs))

    monkeypatch.setattr(logmod, "get_logger", lambda _name: _FakeLog())

    cfg_path = tmp_path / "haiq.toml"
    cfg_path.write_text(
        '[onebot]\nurl = "ws://127.0.0.1:3001"\nplugin_dirs = ["plugins"]\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)

    # 段内写法被丢弃，于是回落到默认值（这正是必须告警的原因：用户以为自己配了）
    assert [Path(p).as_posix() for p in cfg.plugin_dirs] == ["plugins"]
    assert [e for e, _ in seen] == ["config_field_misplaced"]
    assert seen[0][1]["field"] == "plugin_dirs"
    assert seen[0][1]["section"] == "onebot"


def test_warn_misplaced_fields_is_quiet_on_correct_layout(tmp_path: Path, monkeypatch) -> None:  # noqa: E501
    """正常布局（字段在顶层）不该刷警告。"""
    import haiqbot.kernel.log as logmod

    seen: list[str] = []

    class _FakeLog:
        def warning(self, event: str, **kwargs) -> None:
            seen.append(event)

    monkeypatch.setattr(logmod, "get_logger", lambda _name: _FakeLog())
    _warn_misplaced_fields({"plugin_dirs": ["plugins"], "onebot": {"url": "ws://x"}}, tmp_path)
    assert seen == []
