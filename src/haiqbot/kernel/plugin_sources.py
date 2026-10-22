"""插件发现来源：entry point + 本地目录。

内核只认这两种**协议**。这里不出现任何具体插件包名 —— 内置插件同样
通过 entry point 注册，所以「拆包」只是发布形态的变化，不需要改这里。
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .log import get_logger

if TYPE_CHECKING:  # pragma: no cover
    pass

log = get_logger(__name__)

ENTRY_GROUP = "haiqbot.plugins"


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


def local_plugin_dirs(config: Any) -> dict[str, Path]:
    """{插件 id: 目录}，来自 `config.plugin_dirs` 里含 `plugin.toml` 的子目录。"""
    out: dict[str, Path] = {}
    for raw in getattr(config, "plugin_dirs", []) or []:
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
                with manifest_file.open("rb") as fh:
                    data = tomllib.load(fh)
            except Exception:  # noqa: BLE001
                log.warning("plugin_toml_invalid", path=str(manifest_file))
                continue
            out[str(data.get("id") or child.name)] = child
    return out


def iter_plugin_classes(config: Any = None) -> Iterator[type]:
    """逐个产出插件类（只 import 模块，不实例化、不加载 registry）。

    `haiqbot init` 生成默认配置、`haiq secrets` 解析槽位名时用它。
    本地目录插件不在这里 —— 它们需要 registry 的完整加载流程。
    """
    from importlib import import_module

    for value in entry_point_map().values():
        mod_name, _, attr = value.partition(":")
        try:
            module = import_module(mod_name)
        except Exception:  # noqa: BLE001
            continue
        obj = getattr(module, attr, None) if attr else module
        if isinstance(obj, type):
            yield obj
