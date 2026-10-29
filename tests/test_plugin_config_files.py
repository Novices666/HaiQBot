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


# --------------------------------------------------------------------------- #
# 容器部署：HAIQ_DATA_DIR 覆盖 data_dir 时，**读**必须和**写**指向同一个文件
# --------------------------------------------------------------------------- #
def _container_like(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """造出容器的形状：主 toml 里 data_dir 是相对路径，实际数据目录靠环境变量指。

    容器里 `haiq.docker.toml` 写 `data_dir = "data"`（相对），而
    `HAIQ_DATA_DIR=/data` 才是真落点；并且 `.dockerignore` 排除了 `data/`，
    所以镜像里 `/app/data` **不存在**。
    """
    cfg_dir = tmp_path / "app"
    cfg_dir.mkdir()
    cfg = cfg_dir / "haiq.toml"
    cfg.write_text('data_dir = "data"\n[plugins]\nenabled = ["demo"]\n', encoding="utf-8")
    # 容器的工作目录里没有 ./data —— 这正是镜像的真实情况
    monkeypatch.chdir(cfg_dir)
    data = tmp_path / "volume"
    monkeypatch.setenv("HAIQ_DATA_DIR", str(data))
    return cfg, data


def test_reads_plugin_config_from_env_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：环境变量覆盖晚于插件配置读取 → 面板保存的东西重启后变回默认值。

    实测症状（容器里）：面板保存生图/对话参数成功、`/data/plugins/*.toml` 里也确实
    写进去了，但一重启容器就全没了 —— 因为启动时按**覆盖前**的相对 `data`
    去 CWD 下找 `/app/data/plugins/`（不存在），读到空 dict，插件回落到默认值。
    """
    cfg, data = _container_like(tmp_path, monkeypatch)
    target = plugin_config_file(data, "demo")
    target.parent.mkdir(parents=True)
    target.write_text('model = "from-env-dir"\n', encoding="utf-8")

    loaded = load_config(cfg)

    assert loaded.plugin_config.get("demo") == {"model": "from-env-dir"}, "读必须跟环境变量走"


def test_plugin_config_read_and_write_paths_agree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """不变量：读的位置 == 写的位置（面板写的文件和启动读的文件必须是同一个）。"""
    cfg, data = _container_like(tmp_path, monkeypatch)
    target = plugin_config_file(data, "demo")
    target.parent.mkdir(parents=True)
    target.write_text("retries = 2\n", encoding="utf-8")

    loaded = load_config(cfg)

    assert loaded.plugin_config.get("demo") == {"retries": 2}
    # 面板保存时用的是 runtime.config.data_dir，它必须指回同一个目录
    assert plugin_config_file(loaded.data_dir, "demo") == target
