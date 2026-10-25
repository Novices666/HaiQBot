"""异常转人话，并掩掉任何看起来像凭据的片段。

群里要能看出"哪一步失败、为什么"，但绝不能把 key 带出去。
"""

from __future__ import annotations

import re

_SECRET_LIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|(?:api[_-]?key|token|secret)=[^\s&]+)", re.I)


def safe_detail(exc: BaseException, limit: int = 120) -> str:
    """只要原因，不要异常类名。

    群里那句提示是给人看的：`生图接口返回 401` 比
    `ProviderError: 生图接口返回 401` 少一层噪音。类名仍然留在日志里，
    排查时按 trace_id 去捞。
    """
    text = _SECRET_LIKE.sub("<redacted>", str(exc) or "")
    text = " ".join(text.split())
    return text[:limit] or "没有更多信息"


def safe_reason(exc: BaseException) -> str:
    """把异常压成一行：类型 + 概要（≤120 字），凭据形态的片段替换成 <redacted>。"""
    text = _SECRET_LIKE.sub("<redacted>", str(exc) or "")
    text = " ".join(text.split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:120]}"


#: 人设。两条对话路径（router 的判定提示、chat 的 system）必须共用同一份设定，
#: 否则同一个群里会出现两个人格 —— 改名字/性格只改这里。
PERSONA_NAME = "蔡小姬"
PERSONA_TRAITS = "活泼、调皮、可爱，机灵但不刻薄"

#: 消息前缀的协议说明。要同时给到两条对话路径（router 的判定提示、chat 的 system），
#: 否则模型会把「昵称: 」当成噪声，然后凭先验瞎编「我看不到你的昵称」。
SENDER_PREFIX_NOTE = (
    "群里每条用户消息可能以「昵称: 」开头，那只是发言人的标记（可能是群昵称、群名或备注），"
    "不是对话内容的一部分。直接用这个昵称称呼对方就行 —— "
    "不要复述它，不要解释它，不要说你不知道它是什么，也不要声称「看不到昵称」。"
)
