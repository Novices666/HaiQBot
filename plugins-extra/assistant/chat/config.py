"""对话配置模型（归插件所有，内核不认识这些字段）。"""

from __future__ import annotations

from pydantic import BaseModel


class ChatCfg(BaseModel):
    """LLM 对话配置。密钥同样只放键名（`api_key_ref`），真值在 vault。"""

    provider: str = "openai_compat"
    base_url: str = "https://api.openai.com/v1"
    model: str = "deepseek-v4.1-flash"
    api_key_ref: str = "HAIQ_CHAT_API_KEY"
    system_prompt: str = "你是一个简洁友好的群聊助手，用中文回答，控制在 150 字以内。"
    max_tokens: int = 800
    temperature: float = 0.7
    timeout_seconds: float = 90.0
    max_input_chars: int = 2000
    # 多轮上下文：保留最近 N 轮（user+assistant 各算一条）
    history_turns: int = 6
    # 网关抖动（429/5xx）时的重试：retries 为额外尝试次数
    # 实测该网关单次成功率约 62%，503 会成簇出现，所以默认给到 4 次重试
    retries: int = 4
    retry_backoff_seconds: float = 1.0
