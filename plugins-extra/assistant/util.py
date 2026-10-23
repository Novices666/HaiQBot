"""异常转人话，并掩掉任何看起来像凭据的片段。

群里要能看出"哪一步失败、为什么"，但绝不能把 key 带出去。
"""

from __future__ import annotations

import re

_SECRET_LIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|(?:api[_-]?key|token|secret)=[^\s&]+)", re.I)


def safe_reason(exc: BaseException) -> str:
    """把异常压成一行：类型 + 概要（≤120 字），凭据形态的片段替换成 <redacted>。"""
    text = _SECRET_LIKE.sub("<redacted>", str(exc) or "")
    text = " ".join(text.split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:120]}"
