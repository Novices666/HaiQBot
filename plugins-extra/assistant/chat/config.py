"""对话配置模型（归插件所有，内核不认识这些字段）。"""

from __future__ import annotations

from pydantic import BaseModel


class ChatCfg(BaseModel):
    """LLM 对话配置。密钥同样只放键名（`api_key_ref`），真值在 vault。"""

    provider: str = "openai_compat"
    base_url: str = "https://api.openai.com/v1"
    model: str = "deepseek-v4.1-flash"
    api_key_ref: str = "HAIQ_CHAT_API_KEY"
    #: 默认人设 = 蔡小姬。与 router.SYSTEM_PROMPT 共用同一份设定（见 util.PERSONA_*），
    #: 两条路径各写一遍是因为一条是配置、一条是硬编码的判定提示；
    #: tests/test_assistant_persona.py 会断言两边都指向同一个人名，防漂移。
    system_prompt: str = (
        "你叫蔡小姬，群里的人都叫你小姬。你活泼、调皮、有点小机灵，"
        "像个爱逗人开心的姑娘 —— 不是客服，也不是助理。\n"
        "说话用口语，可以用「呀」「啦」「欸」「嘿嘿」这类语气词，偶尔自嘲或撒个小娇，"
        "但别油腻、别每句都卖萌、别堆表情。\n"
        "回答一般 1~3 句，别写小作文，别分点罗列（对方真要步骤时除外）。"
        "正经问题认真答，答完可以顺口贫一句；别人逗你就接梗，但不拿别人的短处开玩笑。\n"
        "不确定的事就直说不确定，别编。"
    )
    max_tokens: int = 800
    temperature: float = 0.7
    timeout_seconds: float = 90.0
    #: 一次对话的**总时限**（秒，含所有重试）。必须明显小于 @我 那个 handler 的
    #: timeout（180s）：内层不限时的话，内核会在半路把 handler 取消，
    #: 用户不但收不到回复，连「对话失败」这一句都收不到 —— 看起来就是「完全没反应」。
    budget_seconds: float = 120.0
    max_input_chars: int = 2000
    # 多轮上下文：保留最近 N 轮（user+assistant 各算一条）
    history_turns: int = 6
    # 网关抖动（429/5xx）时的重试：retries 为额外尝试次数
    # 实测该网关单次成功率约 62%，503 会成簇出现，所以默认给到 4 次重试
    retries: int = 4
    retry_backoff_seconds: float = 1.0


def persona_or(prompt: str | None) -> str:
    """取配置里的 system prompt；留空则回落到内置人设。

    空串必须当成"没填"：LLMBroker 会把空的 system 整条去掉，只剩
    SENDER_PREFIX_NOTE 那一句附加说明 —— 人设静默失效，日志里看不出异常。
    """
    text = (prompt or "").strip()
    return text or ChatCfg().system_prompt
