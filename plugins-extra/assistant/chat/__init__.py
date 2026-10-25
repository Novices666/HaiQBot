"""对话 provider 层。

原先是独立插件 `chat`，现在只是 `assistant` 的内部模块 —— 对外只暴露
`build_service(pctx)`，由 `Assistant` 在加载时组装成 `llm:chat` 服务。
"""

from __future__ import annotations

from typing import Any

from haiqbot.plugin_api import ProviderContext

from ..util import SENDER_PREFIX_NOTE

CHAT_MODELS = ("deepseek-v4.1-flash", "deepseek-v4-flash", "deepseek-v4-pro", "gpt-4o-mini")

#: 凭据槽位名 / 配置前缀（和 image 层同样的约定，见 image/__init__.py）
SLOT = "chat_api_key"
PREFIX = "chat_"


def sub_config(config: Any, prefix: str = PREFIX) -> dict[str, Any]:
    """从扁平配置里摘出属于本层的那几条（按前缀），并剥掉前缀。"""
    items = dict(config or {})
    sub = {k[len(prefix) :]: v for k, v in items.items() if k.startswith(prefix)}
    # 内核的演示模式对所有插件统一下发同一个 `provider` 键（它不该认识
    # image_/chat_ 这种前缀），所以这里让它生效。
    if "provider" not in sub and "provider" in items:
        sub["provider"] = items["provider"]
    return sub


def build_service(pctx: ProviderContext) -> Any:
    """组装对话服务。凭据只通过槽位取，provider 本身不接触密钥文本。"""
    from .broker import LLMBroker
    from .config import ChatCfg, persona_or
    from .providers.mock import MockChatProvider
    from .providers.openai_compat import OpenAICompatChatProvider

    cfg = ChatCfg.model_validate(sub_config(pctx.config))
    if cfg.provider == "mock":
        provider: Any = MockChatProvider()
    else:
        provider = OpenAICompatChatProvider(pctx.slot(SLOT).headers, cfg)

    broker = LLMBroker(
        provider,
        model=cfg.model,
        system_prompt=persona_or(cfg.system_prompt),
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
    )
    broker.max_input_chars = cfg.max_input_chars
    broker.system_extra = SENDER_PREFIX_NOTE
    return broker
