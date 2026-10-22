"""插件发现来源：entry point + 本地目录。

内核只认这两种**协议**，代码里不出现任何具体插件名。随仓库分发的插件同样
放在 `plugin_dirs` 指向的目录里（默认 `plugins/`），和第三方插件走同一条路 ——
所以「插件搬出内核」不需要改这里，也不需要重新安装框架。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import toml_io
from .log import get_logger
from .plugin import Plugin

log = get_logger(__name__)

ENTRY_GROUP = "haiqbot.plugins"
# 没配置 `plugin_dirs` 时扫这里。相对路径按**进程工作目录**解析。
DEFAULT_PLUGIN_DIR = Path("plugins")


def default_plugin_dirs() -> list[Path]:
    return [DEFAULT_PLUGIN_DIR]


def entry_point_map(group: str = ENTRY_GROUP) -> dict[str, str]:
    """{插件 id: "module:attr"}，来自已安装的 entry point。"""
    out: dict[str, str] = {}
    try:
        from importlib.metadata import entry_points
    except Exception:  # noqa: BLE001
        return out
    try:
        eps = entry_points(group=group)
    except Exception:  # noqa: BLE001
        return out
    for ep in eps:
        out[ep.name] = ep.value
    return out


def local_plugin_dirs(config: Any = None) -> dict[str, Path]:
    """{插件 id: 目录}，来自 `config.plugin_dirs` 里含 `plugin.toml` 的子目录。

    `config` 为 None 时用默认目录；显式配置成空列表表示**不扫本地目录**。
    """
    raw_dirs = getattr(config, "plugin_dirs", None) if config is not None else None
    if raw_dirs is None:
        raw_dirs = default_plugin_dirs()

    out: dict[str, Path] = {}
    for raw in raw_dirs or []:
        base = Path(raw)
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if not child.is_dir():
                continue
            manifest_file = child / "plugin.toml"
            if not manifest_file.is_file():
                continue
            try:
                data = toml_io.load_file(manifest_file)
            except Exception as exc:  # noqa: BLE001 - 单个插件坏了不该拖垮发现流程
                log.warning(
                    "plugin_toml_invalid",
                    path=str(manifest_file),
                    error=type(exc).__name__,
                    hint="plugin.toml 必须是合法 UTF-8 TOML（BOM 已容忍，语法错需自己修）",
                )
                continue
            out[str(data.get("id") or child.name)] = child
    return out


def load_local_module(pid: str, path: Path) -> Any:
    """按「本地目录插件」协议 import 一个插件目录，返回模块。

    模块名固定为 `haiqbot_local_<pid>` 并注册进 `sys.modules`，所以插件内部的
    相对 import（`from .broker import ...`）能正常工作。
    """
    mod_name = f"haiqbot_local_{pid}"
    init_file = path / "__init__.py"
    if not init_file.is_file():
        raise FileNotFoundError(f"本地插件 {pid} 缺少 __init__.py: {path}")

    resolved = str(init_file.resolve())
    existing = sys.modules.get(mod_name)
    if existing is not None:
        if getattr(existing, "__haiq_plugin_dir__", None) == resolved:
            return existing
        # 同名模块但来自另一个目录（测试里常见）：丢掉重载，别串味
        sys.modules.pop(mod_name, None)

    spec = importlib.util.spec_from_file_location(mod_name, init_file, submodule_search_locations=[str(path)])
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载本地插件 {pid}: {path}")
    module = importlib.util.module_from_spec(spec)
    module.__haiq_plugin_dir__ = resolved
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    return module


def pick_plugin_class(target: Any, pid: str) -> type[Plugin]:
    """从模块（或类）里挑出这个插件对应的 Plugin 子类。"""
    if inspect.isclass(target) and issubclass(target, Plugin) and target is not Plugin:
        return target
    candidates = [
        obj
        for _, obj in inspect.getmembers(target, inspect.isclass)
        if issubclass(obj, Plugin) and obj is not Plugin
    ]
    for cls in candidates:
        if getattr(cls, "id", None) == pid:
            return cls
    if len(candidates) == 1:
        return candidates[0]
    raise LookupError(f"插件 {pid} 中没有找到唯一的 Plugin 子类")


def iter_plugin_classes(config: Any = None) -> Iterator[type]:
    """逐个产出插件类（只 import 模块，不实例化、不加载 registry）。

    顺序：entry point 优先，其次本地目录；同一个 id 只产出一次。
    `haiqbot init` 生成默认配置、`haiq secrets` 解析槽位名时用它 ——
    这两条路径没有完整 runtime，所以必须在这里也能扫到本地目录插件。
    """
    seen: set[str] = set()

    for value in entry_point_map().values():
        mod_name, _, attr = value.partition(":")
        try:
            from importlib import import_module

            module = import_module(mod_name)
        except Exception:  # noqa: BLE001 - 装坏的入口不该拖垮发现
            continue
        target = getattr(module, attr, None) if attr else module
        if not (inspect.isclass(target) and issubclass(target, Plugin)) or target is Plugin:
            continue
        pid = str(getattr(target, "id", "") or "")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        yield target

    for pid, path in local_plugin_dirs(config).items():
        if pid in seen:
            continue
        try:
            module = load_local_module(pid, path)
            cls = pick_plugin_class(module, pid)
        except Exception as exc:  # noqa: BLE001 - 坏插件只跳过它自己
            log.warning(
                "plugin_local_import_failed",
                plugin=pid,
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
            )
            continue
        seen.add(pid)
        yield cls
