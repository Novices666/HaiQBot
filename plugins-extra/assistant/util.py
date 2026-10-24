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


#: 消息前缀的协议说明。要同时给到两条对话路径（router 的判定提示、chat 的 system），
#: 否则模型会把「昵称: 」当成噪声，然后凭先验瞎编「我看不到你的昵称」。
SENDER_PREFIX_NOTE = (
    "群里每条用户消息可能以「昵称: 」开头，那是发言人的群昵称，不是对话内容的一部分。"
    "不要复述这个前缀；需要称呼对方时用这个昵称。你只掌握这些昵称，不掌握账号资料，"
    "不要声称「看不到昵称」。"
)
