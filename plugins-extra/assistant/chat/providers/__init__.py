"""chat 自己的 provider 实现。"""

from .mock import MockChatProvider
from .openai_compat import OpenAICompatChatProvider

__all__ = ["MockChatProvider", "OpenAICompatChatProvider"]
