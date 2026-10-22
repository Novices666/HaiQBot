"""painter 自己的 provider 实现。"""

from .mock import MockImageProvider
from .openai_compat import OpenAICompatProvider

__all__ = ["MockImageProvider", "OpenAICompatProvider"]
