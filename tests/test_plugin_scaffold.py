"""`haiq plugin new` 的骨架生成：文件内容、enabled 写入、拒绝非法输入。"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from haiqbot.kernel.plugin import PLUGIN_API_VERSION
from haiqbot.kernel.scaffold import ScaffoldError, class_name, scaffold_plugin


def _config(tmp_path: Path, enabled: list[str] | None = None) -> Path:
    cfg = tmp_path / "haiq.toml"
    body = "[plugins]\n"
    body += 'plugin_dirs = ["plugins"]\n'
    body += f"enabled = {enabled or []!r}\n".replace("'", '"')
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _enabled(cfg: Path) -> list[str]:
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    return list(data["plugins"]["enabled"])


def test_creates_importable_plugin(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["echo"])
    res = scaffold_plugin("weather", base_dir="plugins", config_path=cfg, enabled=["echo"])

    assert res.plugin_dir == (tmp_path / "plugins" / "weather").resolve()
    assert res.enabled_added is True
    init_py = (res.plugin_dir / "__init__.py").read_text(encoding="utf-8")
    toml = (res.plugin_dir / "plugin.toml").read_text(encoding="utf-8")

    # 生成的代码必须能被 import，且插件 id / 命令都已就位
    assert "class Weather(Plugin):" in init_py
    assert 'id = "weather"' in init_py
    assert '@on_command("weather"' in init_py
    assert "$" not in init_py, "模板占位符没替换干净"
    compile(init_py, str(res.plugin_dir / "__init__.py"), "exec")

    assert 'id = "weather"' in toml
    assert "api_version = " in toml
    assert "$" not in toml


def test_api_version_follows_kernel_constant(tmp_path: Path) -> None:
    """模板里的 api_version 必须来自内核常量，不能硬编码 —— 否则升版即失效。"""
    cfg = _config(tmp_path)
    res = scaffold_plugin("probe", base_dir="plugins", config_path=cfg, enabled=[])
    data = tomllib.loads((res.plugin_dir / "plugin.toml").read_text(encoding="utf-8"))
    assert data["api_version"] == PLUGIN_API_VERSION


def test_adds_to_enabled_without_touching_others(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["echo", "help"])
    scaffold_plugin("weather", base_dir="plugins", config_path=cfg, enabled=["echo", "help"])
    assert _enabled(cfg) == ["echo", "help", "weather"]


def test_already_enabled_is_not_duplicated(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["echo"])
    res = scaffold_plugin("echo2", base_dir="plugins", config_path=cfg, enabled=["echo", "echo2"], force=True)
    assert res.enabled_added is False
    # 调用方说「已经在 enabled 里」时不该重复写文件；配置文件保持原样
    assert _enabled(cfg) == ["echo"]


def test_refuses_existing_dir_without_force(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    scaffold_plugin("dup", base_dir="plugins", config_path=cfg, enabled=[])
    with pytest.raises(ScaffoldError, match="已存在"):
        scaffold_plugin("dup", base_dir="plugins", config_path=cfg, enabled=["dup"])


def test_force_overwrites_generated_files(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    res = scaffold_plugin("dup", base_dir="plugins", config_path=cfg, enabled=[])
    (res.plugin_dir / "__init__.py").write_text("broken", encoding="utf-8")
    scaffold_plugin("dup", base_dir="plugins", config_path=cfg, enabled=["dup"], force=True)
    assert "class Dup(Plugin):" in (res.plugin_dir / "__init__.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["", "Weather", "wea-ther", "1abc", "a" * 33, "有中文"])
def test_rejects_bad_ids(tmp_path: Path, bad: str) -> None:
    cfg = _config(tmp_path)
    with pytest.raises(ScaffoldError, match="不合法"):
        scaffold_plugin(bad, base_dir="plugins", config_path=cfg, enabled=[])


def test_missing_config_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ScaffoldError, match="找不到配置文件"):
        scaffold_plugin("x", base_dir="plugins", config_path=tmp_path / "nope.toml", enabled=[])


def test_relative_dir_resolves_against_config_location(tmp_path: Path) -> None:
    """从别的目录执行时，相对 plugin_dirs 应跟着配置文件走，而不是跟着 CWD。"""
    nested = tmp_path / "proj"
    nested.mkdir()
    cfg = _config(nested)
    res = scaffold_plugin("relocated", base_dir="plugins", config_path=cfg, enabled=[])
    assert res.plugin_dir.parent.parent == nested


def test_class_name_from_snake_case() -> None:
    assert class_name("my_plugin") == "MyPlugin"
    assert class_name("weather") == "Weather"
