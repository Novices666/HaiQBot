"""架构边界：内核不得硬编码插件包名；插件不得伸手进内核内部模块。

这两条一旦回潮，「核心与插件分离」就只是口号。
"""

from __future__ import annotations

import re
from pathlib import Path

import haiqbot
from haiqbot.config import HaiqConfig

PKG_ROOT = Path(haiqbot.__file__).parent
PLUGIN_PKG = "haiqbot.builtin_plugins"


def _iter_kernel_files():
    for path in PKG_ROOT.rglob("*.py"):
        rel = path.relative_to(PKG_ROOT)
        if rel.parts and rel.parts[0] == "builtin_plugins":
            continue  # 插件自己的代码不算内核
        yield rel, path


def test_kernel_does_not_hardcode_plugin_package() -> None:
    """内核代码里不能出现插件包名。

    唯一豁免是 `config.py` —— 那里是**可改的默认配置值**，不是代码依赖。
    """
    offenders = []
    for rel, path in _iter_kernel_files():
        if rel.as_posix() == "config.py":
            continue
        if PLUGIN_PKG in path.read_text(encoding="utf-8"):
            offenders.append(rel.as_posix())
    assert not offenders, f"内核仍硬编码插件包名：{offenders}"


def test_plugin_package_is_configurable() -> None:
    cfg = HaiqConfig(builtin_plugins_package="")
    assert cfg.builtin_plugins_package == ""
    # 默认值仍然指向随框架分发的插件
    assert HaiqConfig().builtin_plugins_package == PLUGIN_PKG


_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(haiqbot[\w.]*)", re.MULTILINE)


def test_plugins_only_import_public_api() -> None:
    """插件只允许 import `haiqbot.plugin_api`（其余用相对 import 走自己包）。"""
    allowed = {"haiqbot.plugin_api"}
    offenders: list[str] = []
    for path in (PKG_ROOT / "builtin_plugins").rglob("*.py"):
        for target in _IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            root = ".".join(target.split(".")[:2])
            if root in allowed:
                continue
            offenders.append(f"{path.relative_to(PKG_ROOT).as_posix()} -> {target}")
    assert not offenders, "插件伸手进了内核：\n" + "\n".join(sorted(set(offenders)))


def test_plugin_discovery_goes_through_entry_points() -> None:
    """发现路径：entry point（协议）+ 本地目录（配置），不靠扫包。"""
    from haiqbot.kernel.plugin_sources import entry_point_map

    eps = entry_point_map()
    assert {"painter", "chat", "echo", "help", "admin"} <= set(eps)
