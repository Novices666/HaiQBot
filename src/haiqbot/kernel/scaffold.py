"""`haiq plugin new` 的实现：把「建目录 + 写清单 + 进 enabled」压成一条命令。

放在内核里而不是 `cli.py`，是因为它不涉及 argparse，可以被直接测试。

设计取舍：
- **不改内核任何语义**：生成的插件和手写的插件走完全同一条加载路径
  （`plugin_dirs` 本地目录，`source=local`）。
- **默认就进 `enabled`**：新人最常卡的一步就是「目录建了、频道也勾了，却忘了
  写进 `[plugins] enabled`」，于是整条链路静默不响应。脚手架直接替他把这步做掉。
- **`api_version` 由 `PLUGIN_API_VERSION` 注入**：模板里绝不硬编码 1，
  否则内核升版本后新脚手架会生成一个立刻被拒绝加载的插件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any

from haiqbot.kernel.config_writer import write_config
from haiqbot.kernel.plugin import PLUGIN_API_VERSION

# 插件 id 同时是目录名、`[plugins.<id>]` 段名和命令名，所以收紧到
# 「小写字母开头 + 小写字母/数字/下划线」：既能当 Python 标识符片段，也不会
# 在 TOML 段名里需要转义。
PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: 建目录 / 写文件时不覆盖已有内容的提示
_EXISTS_HINT = "已存在。换个 id，或加 --force 覆盖（只覆盖本工具生成的两个文件）"

_INIT_TEMPLATE = Template(
    '''"""$name：一句话说清这个插件做什么。

改完代码必须重启 `haiq run` —— 插件是启动时 import 的，没有热重载。
"""

from __future__ import annotations

from haiqbot import Context, Plugin, on_command


class $cls(Plugin):
    id = "$pid"
    name = "$name"
    version = "0.1.0"

    @on_command("$pid", usage="$pid", priority=5)
    async def run(self, ctx: Context) -> None:
        await ctx.reply.text("hello from $pid")
'''
)

_TOML_TEMPLATE = Template(
    """# 本地目录插件的清单：内核读它拿元信息（也会被 __init__.py 里的类属性覆盖）。
# 想加凭据就用 secrets = { "api_key": SecretSlot(...) }，见 docs/PLUGIN-GUIDE.md。
id = "$pid"
name = "$name"
version = "0.1.0"
description = "$name（本地目录插件）"
api_version = $api_version
isolation = "inline"
"""
)


class ScaffoldError(Exception):
    """脚手架失败（id 非法、目录已存在、配置文件缺失……）。"""


@dataclass(frozen=True)
class ScaffoldResult:
    plugin_id: str
    plugin_dir: Path
    files: list[Path]
    enabled_added: bool
    config_path: Path


def class_name(pid: str) -> str:
    """`my_plugin` -> `MyPlugin`。"""
    parts = [p for p in re.split(r"[^0-9a-zA-Z]+", pid) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Plugin"


def _resolve_base(base_dir: Any, config_path: Path) -> Path:
    """插件根目录。相对路径按**配置文件所在目录**解析，和 `haiq run` 的直觉一致。"""
    base = Path(base_dir) if base_dir else Path("plugins")
    if base.is_absolute():
        return base
    return (config_path.parent / base).resolve()


def scaffold_plugin(
    plugin_id: str,
    *,
    base_dir: Any,
    config_path: Path,
    enabled: list[str],
    force: bool = False,
) -> ScaffoldResult:
    """生成插件骨架，并把它加进 `[plugins] enabled`。"""
    pid = str(plugin_id or "").strip()
    if not PLUGIN_ID_RE.match(pid):
        raise ScaffoldError(
            f"插件 id 不合法：{pid!r}。要求：小写字母开头，只含小写字母/数字/下划线，最多 32 字符"
        )

    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        raise ScaffoldError(f"找不到配置文件 {cfg_path} —— 先跑 `haiq init`")

    root = _resolve_base(base_dir, cfg_path)
    target = root / pid
    if target.exists() and not force:
        raise ScaffoldError(f"{target} {_EXISTS_HINT}")

    target.mkdir(parents=True, exist_ok=True)
    name = pid.replace("_", " ").strip() or pid
    init_py = target / "__init__.py"
    toml = target / "plugin.toml"
    init_py.write_text(
        _INIT_TEMPLATE.substitute(pid=pid, cls=class_name(pid), name=name),
        encoding="utf-8",
    )
    toml.write_text(
        _TOML_TEMPLATE.substitute(pid=pid, name=name, api_version=PLUGIN_API_VERSION),
        encoding="utf-8",
    )

    new_enabled = list(enabled or [])
    added = pid not in new_enabled
    if added:
        new_enabled.append(pid)
        write_config(cfg_path, {"plugins.enabled": new_enabled})

    return ScaffoldResult(
        plugin_id=pid,
        plugin_dir=target,
        files=[init_py, toml],
        enabled_added=added,
        config_path=cfg_path,
    )
