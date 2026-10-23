"""生图配置模型（归插件所有，内核不认识这些字段）。"""

from __future__ import annotations

from pydantic import BaseModel


class ImageCfg(BaseModel):
    provider: str = "openai_compat"
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-image-1"
    # vault 中的键名，不是密钥本身
    api_key_ref: str = "HAIQ_IMAGE_API_KEY"
    size: str = "1024x1024"
    timeout_seconds: float = 180.0
    max_prompt_chars: int = 800
