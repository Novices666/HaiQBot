"""架构边界：内核里**一个插件都不许有**；插件不许伸手进内核内部模块。

这两条一旦回潮，「内核与插件分离」就只是口号 —— 所以用测试钉死，
而不是靠人记得。
"""

from __future__ import annotations

import re
from pathlib import Path

import haiqbot
from haiqbot.config import HaiqConfig

PKG_ROOT = Path(haiqbot.__file__).parent
REPO_ROOT = PKG_ROOT.parent.parent
FRAMEWORK_PLUGINS_DIR = REPO_ROOT / "plugins"
EXTRA_PLUGINS_DIR = REPO_ROOT / "plugins-extra"
ALL_PLUGIN_DIRS = [FRAMEWORK_PLUGINS_DIR, EXTRA_PLUGINS_DIR]

#: 框架自带的最小集 —— 判断标准：陌生人装好、不接任何外部服务，是否仍然想要它。
SHIPPED_PLUGIN_IDS = {"echo", "help"}

#: 自有插件 —— 不属于框架，谁想要谁自己接（见 plugins-extra/README.md）。
EXTRA_PLUGIN_IDS = {"admin", "assistant", "guard"}

# 历史包袱：这里曾经是内置插件的家。留着这个名字就会被误认为还能放插件。
FORBIDDEN_IN_KERNEL = "builtin_plugins"


def _iter_kernel_files():
    for path in PKG_ROOT.rglob("*.py"):
        yield path.relative_to(PKG_ROOT), path


def test_kernel_ships_no_plugin_code() -> None:
    """内核包里不许有插件目录、插件清单或插件包标记。"""
    offenders: list[str] = []
    if (PKG_ROOT / FORBIDDEN_IN_KERNEL).exists():
        offenders.append(f"{FORBIDDEN_IN_KERNEL}/ 仍然存在于内核包里")
    for rel, _ in _iter_kernel_files():
        if rel.parts and rel.parts[0] == FORBIDDEN_IN_KERNEL:
            offenders.append(rel.as_posix())
        if rel.name == "plugin.toml":
            offenders.append(rel.as_posix())
    assert not offenders, "内核里还有插件：" + "、".join(sorted(set(offenders)))


def test_kernel_never_mentions_plugin_package() -> None:
    """内核源码里连这个名字都不该出现（连默认配置值里也不行）。"""
    offenders = [
        rel.as_posix()
        for rel, path in _iter_kernel_files()
        if FORBIDDEN_IN_KERNEL in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"内核仍在提插件包名：{offenders}"


def test_default_config_assumes_no_plugins() -> None:
    """开箱配置：不启用任何插件，只声明去哪找。"""
    cfg = HaiqConfig()
    assert cfg.plugins_enabled == []
    assert [Path(p).as_posix() for p in cfg.plugin_dirs] == ["plugins", "plugins-extra"]


def test_shipped_plugins_live_outside_kernel() -> None:
    """框架自带的最小集在 `plugins/` 里，各自带 plugin.toml。"""
    assert FRAMEWORK_PLUGINS_DIR.is_dir(), f"缺少插件目录：{FRAMEWORK_PLUGINS_DIR}"
    missing = sorted(
        pid for pid in SHIPPED_PLUGIN_IDS if not (FRAMEWORK_PLUGINS_DIR / pid / "plugin.toml").is_file()
    )
    assert not missing, f"这些插件缺 plugin.toml：{missing}"


def test_personal_plugins_are_not_in_framework_dir() -> None:
    """个人集成不许混进 `plugins/` —— 框架自带的目录只放最小集。

    这条是「内置插件瘦身」的防回潮钉子：以后往 `plugins/` 里加任何绑定外部服务
    或个人取舍的插件，这里会立刻红。
    """
    strays = sorted(
        pid for pid in EXTRA_PLUGIN_IDS if (FRAMEWORK_PLUGINS_DIR / pid / "plugin.toml").is_file()
    )
    assert not strays, f"这些个人插件混进了框架目录 plugins/：{strays}"
    missing = sorted(
        pid for pid in EXTRA_PLUGIN_IDS if not (EXTRA_PLUGINS_DIR / pid / "plugin.toml").is_file()
    )
    assert not missing, f"个人插件缺 plugin.toml：{missing}"


_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(haiqbot[\w.]*)", re.MULTILINE)


def test_plugins_only_import_public_api() -> None:
    """插件只允许 import `haiqbot.plugin_api`（其余用相对 import 走自己包）。"""
    allowed = {"haiqbot.plugin_api"}
    offenders: list[str] = []
    for path in (p for base in ALL_PLUGIN_DIRS for p in base.rglob("*.py")):
        for target in _IMPORT_RE.findall(path.read_text(encoding="utf-8")):
            root = ".".join(target.split(".")[:2])
            if root in allowed:
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {target}")
    assert not offenders, "插件伸手进了内核：\n" + "\n".join(sorted(set(offenders)))


def test_plugin_discovery_uses_local_dirs() -> None:
    """默认发现路径只认 `plugins/`：个人集成不会被开箱带进来。"""
    from haiqbot.kernel.plugin_sources import local_plugin_dirs

    found = set(local_plugin_dirs())
    # 默认两个目录：框架目录 + 自有目录。两者都要能发现，否则「插件放进去没反应」。
    assert SHIPPED_PLUGIN_IDS <= found, f"本地目录发现不到：{sorted(SHIPPED_PLUGIN_IDS - found)}"
    assert EXTRA_PLUGIN_IDS <= found, f"自有目录发现不到：{sorted(EXTRA_PLUGIN_IDS - found)}"


def test_local_plugin_dir_without_manifest_is_ignored(tmp_path: Path) -> None:
    """只有带 plugin.toml 的子目录才算插件 —— 顺手放过 .git / __pycache__。"""
    from haiqbot.kernel.plugin_sources import local_plugin_dirs

    (tmp_path / "notaplugin").mkdir()
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "plugin.toml").write_text('id = "real"\n', encoding="utf-8")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "plugin.toml").write_text("这不是 toml [[[", encoding="utf-8")

    found = local_plugin_dirs(HaiqConfig(plugin_dirs=[tmp_path]))
    assert set(found) == {"real"}, found


def test_dump_default_config_lists_discovered_plugins(tmp_path: Path, monkeypatch) -> None:
    """`haiqbot init` 的 enabled 清单来自实际发现，不是写死的常量。"""
    from haiqbot.config import dump_default_config

    monkeypatch.chdir(tmp_path)
    empty = dump_default_config()
    assert 'plugin_dirs = ["plugins", "plugins-extra"]' in empty
    assert "enabled = []" in empty, "空目录下不该凭空列出一堆插件"

    plugin_dir = tmp_path / "plugins" / "demo"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.toml").write_text('id = "demo"\n', encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(
        'from haiqbot.plugin_api import Plugin\n\n\nclass Demo(Plugin):\n    id = "demo"\n    name = "演示"\n',
        encoding="utf-8",
    )

    filled = dump_default_config()
    assert 'enabled = ["demo"]' in filled, "发现到的插件应当被列进模板"
