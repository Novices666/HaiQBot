"""provider 通用抽象（厂商实现归各插件所有）。"""

from .base import ImageProvider, ProviderError

__all__ = ["ImageProvider", "ProviderError"]
