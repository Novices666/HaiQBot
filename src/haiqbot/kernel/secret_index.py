"""凭据槽位索引：`{插件.槽位: 元信息}` 与「槽位名 → 物理键名」解析。

CLI（`haiq secrets`）与面板（`/api/secrets`）共用这一份，避免两边各写一遍然后漂移。
纯逻辑：不碰文件、不读环境变量、不碰 vault。
"""

from __future__ import annotations

from typing import Any

from .plugin import SecretSlot, resolve_secret_ref
from .plugin_sources import iter_plugin_classes


def slot_index(config: Any = None) -> dict[str, dict[str, Any]]:
    """{`插件.槽位`: {plugin, name, slot}}，来源是各插件声明的 `secrets`。"""
    found: dict[str, dict[str, Any]] = {}
    for cls in iter_plugin_classes(config):
        pid = getattr(cls, "id", None)
        slots = getattr(cls, "secrets", None) or {}
        if not pid or not isinstance(slots, dict):
            continue
        for name, slot in slots.items():
            if isinstance(slot, SecretSlot):
                found[f"{pid}.{name}"] = {"plugin": pid, "name": name, "slot": slot}
    return found


def resolve_target(config: Any, arg: str) -> tuple[str, str]:
    """`painter.api_key` → (物理键名, 展示名)；裸键名原样返回两边。"""
    if "." in arg:
        entry = slot_index(config).get(arg)
        if entry is not None:
            section: Any = {}
            if config is not None:
                section = getattr(config, "plugin_config", {}).get(entry["plugin"]) or {}
            physical = resolve_secret_ref(entry["plugin"], entry["name"], entry["slot"], section)
            return physical, arg
    return arg, arg
