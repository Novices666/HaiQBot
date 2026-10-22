"""配置写回：在**保留注释与原排版**的前提下，改动 haiq.toml 里的白名单行。

为什么不用 `tomli_w`：它会整份重新序列化，把用户的注释、分节顺序、空行
全部洗掉。配置文件是手写维护的，那份注释本身就是文档。

所以这里走文本级补丁：
- 只在目标 `[section]` 内找 `key = ...` 那一行，原地替换（保留行尾注释）
- 找不到就在该 section 末尾插入
- 整个 section 不存在才新建

**只处理白名单字段** —— 调用方传进来的路径必须已经过校验，
本模块不做任何语义判断，只负责"把值写进正确的行"。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# 只匹配单层 [section]，不碰 [[array.of.tables]]
_HEADER_RE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*$")


def format_value(value: Any) -> str:
    """把 Python 值渲染成 TOML 字面量。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(format_value(v) for v in value) + "]"
    # json.dumps 的双引号转义规则与 TOML basic string 兼容
    return json.dumps(str(value), ensure_ascii=False)


def _comment_index(line: str) -> int:
    """返回行内注释 `#` 的下标（跳过字符串与数组内部），无注释返回 -1。"""
    in_string = False
    escaped = False
    depth = 0
    for i, ch in enumerate(line):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "#" and depth == 0:
            return i
    return -1


def _with_trailing_comment(old_line: str, new_body: str) -> str:
    idx = _comment_index(old_line)
    if idx < 0:
        return new_body
    return f"{new_body}  {old_line[idx:].strip()}"


def set_value(text: str, section: str, key: str, value: Any) -> str:
    """在 `text` 的 `[section]` 内把 `key` 设为 `value`。

    `section` 传空串表示**顶层键**（TOML 里写在第一个 `[header]` 之前），
    例如 `log_level` / `data_dir`。
    """
    lines = text.splitlines()
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if not m:
            continue
        if m.group(1) == section:
            start = i
            continue
        if start is not None:
            end = i
            break

    rendered = f"{key} = {format_value(value)}"

    if section == "":
        # 顶层：只在前导块里找（第一个 [header] 之前）
        first_header = next((i for i, ln in enumerate(lines) if _HEADER_RE.match(ln)), len(lines))
        key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
        for i in range(first_header):
            if key_re.match(lines[i]):
                lines[i] = _with_trailing_comment(lines[i], rendered)
                return "\n".join(lines) + "\n"
        insert_at = first_header
        while insert_at > 0 and not lines[insert_at - 1].strip():
            insert_at -= 1
        lines.insert(insert_at, rendered)
        return "\n".join(lines) + "\n"

    if start is None:
        # 整节不存在：补空行后新建
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(rendered)
        return "\n".join(lines) + "\n"

    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start + 1, end):
        if key_re.match(lines[i]):
            lines[i] = _with_trailing_comment(lines[i], rendered)
            return "\n".join(lines) + "\n"

    # 节内没这行：插到该节末尾（跳过尾部空行）
    insert_at = end
    while insert_at > start + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, rendered)
    return "\n".join(lines) + "\n"


def set_many(text: str, changes: dict[str, Any]) -> str:
    """`changes` 形如 `{"chat.model": "x", "quota.enabled": False}`。

    不含点号的键视为顶层键（如 `log_level`）。
    """
    for dotted, value in changes.items():
        section, _, key = dotted.rpartition(".")
        if not key:
            raise ValueError(f"配置路径不合法：{dotted!r}")
        text = set_value(text, section, key, value)
    return text


def take_section(text: str, section: str) -> tuple[str | None, str]:
    """把 `[section]` 整块（含头行）取出来，并从原文里删掉。

    返回 `(块文本 | None, 新全文)`。用于把旧版内核段搬到插件段。
    """
    lines = text.splitlines()
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if not m:
            continue
        if m.group(1) == section:
            start = i
            continue
        if start is not None:
            end = i
            break
    if start is None:
        return None, text

    block = "\n".join(lines[start:end]).rstrip()
    rest = lines[:start] + lines[end:]
    while rest and not rest[-1].strip():
        rest.pop()
    return block, "\n".join(rest) + "\n"


def write_config(path: str | Path, changes: dict[str, Any]) -> Path:
    """把 `changes` 写回配置文件。调用方负责保证 `changes` 已过白名单校验。"""
    target = Path(path)
    original = target.read_text(encoding="utf-8") if target.is_file() else ""
    updated = set_many(original, changes)
    if updated != original:
        target.write_text(updated, encoding="utf-8")
    return target
