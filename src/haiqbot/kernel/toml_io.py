"""TOML 读取工具：容忍 Windows 编辑器写入的 UTF-8 BOM。

`tomllib` 严格按规范解析，文件开头只要有三个字节 `EF BB BF` 就直接报
`Invalid statement (at line 1, column 1)`。而 Windows PowerShell 5.1 的
`Set-Content -Encoding utf8`、以及不少编辑器的「另存为 UTF-8」默认**会**带 BOM。

实测后果分两种，都很难自查：

- `haiq.toml` 带 BOM → 抛裸 traceback 启动失败；
- `plugin.toml` 带 BOM → 只留一条 warning，插件静默消失。

所以全仓统一从这里读 TOML：先剥 BOM，再交给 tomllib。
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

BOM = b"\xef\xbb\xbf"


def loads(raw: bytes) -> dict[str, Any]:
    """解析 TOML 字节串，自动剥掉 UTF-8 BOM。"""
    if raw.startswith(BOM):
        raw = raw[len(BOM) :]
    return tomllib.loads(raw.decode("utf-8"))


def load_file(path: str | Path) -> dict[str, Any]:
    """读取并解析一个 TOML 文件（容忍 BOM）。"""
    return loads(Path(path).read_bytes())


def has_bom(path: str | Path) -> bool:
    """文件是否带 UTF-8 BOM；用于给用户一句「换个保存方式」的提示。"""
    return Path(path).read_bytes()[:3] == BOM
