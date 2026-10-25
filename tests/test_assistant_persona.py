"""人设一致性：两条对话路径必须指向同一个人，且配置留空不能把人设整条吞掉。

背景：群里的话走 `router.SYSTEM_PROMPT`（硬编码的判定提示），
`/问` 和 `@机器人` 走 `ChatCfg().system_prompt`（配置默认值）。
两条是分开维护的，历史上就是"一个自称助手、一个没名字"，群里看着像两个人。
"""

from __future__ import annotations

from haiqbot_local_assistant import router, util
from haiqbot_local_assistant.chat.config import ChatCfg, persona_or


def test_both_paths_use_the_same_persona_name() -> None:
    """改名只改 util.PERSONA_NAME 是不够的 —— 两条路径都得落到同一个人。"""
    assert util.PERSONA_NAME in router.SYSTEM_PROMPT
    assert util.PERSONA_NAME in ChatCfg().system_prompt


def test_persona_has_traits_not_just_adjectives_like_nice() -> None:
    """只写"友好"等于没写：模型会退回客服腔。要有具体性格词。"""
    text = ChatCfg().system_prompt
    for trait in ("活泼", "调皮"):
        assert trait in text, trait
    # 反面约束也得在，否则模型会一路卖萌到油腻
    assert "别油腻" in text


def test_blank_system_prompt_falls_back_to_persona() -> None:
    """配置留空 = 用内置人设，而不是让 system 消息整条消失。

    空串会让 LLMBroker 的 system 只剩 SENDER_PREFIX_NOTE 那一句附加说明，
    人设静默失效 —— 而这种失效在小群里几乎不可能被察觉。
    """
    for blank in ("", "   ", "\n", None):
        assert util.PERSONA_NAME in persona_or(blank), repr(blank)


def test_custom_prompt_is_not_overridden() -> None:
    """写了自定义人设就用自定义的 —— 回落只该在"没填"时发生。"""
    assert persona_or("你是一个严肃的文档助手") == "你是一个严肃的文档助手"


def test_router_prompt_has_no_hard_length_cap() -> None:
    """reply 不再写死字数：实测放宽后判定更稳，reply 长几字不影响正确性。"""
    assert "15~30 字" not in router.SYSTEM_PROMPT
