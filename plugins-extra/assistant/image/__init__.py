"""生图 provider 层。

原先是独立插件 `painter`，现在只是 `assistant` 的内部模块 —— 对外只暴露
`build_service(pctx)`，由 `Assistant` 在加载时组装成 `image:generate` 服务。

为什么不直接合并成一个文件：provider（厂商 HTTP 细节）和插件逻辑（命令、审查、
路由）是两种变化频率完全不同的东西，混在一起以后改哪边都要读全文。
"""

from __future__ import annotations

from typing import Any

from haiqbot.plugin_api import ProviderContext

#: 允许的档位。用户要的比例会**吸附**到这几个里最接近的（接口只认这些）。
IMAGE_SIZES = ("1024x1024", "1536x1024", "1024x1536", "512x512", "auto")

IMAGE_MODELS = ("gpt-image-1", "gpt-image-2", "gpt-image-2.5", "dall-e-3")

#: 凭据槽位名 / 配置前缀。配置写成 `[plugins.assistant] image_model = ...`，
#: 取子配置时剥掉前缀再交给 `ImageCfg` —— 一个段里放两套配置，靠前缀区分。
SLOT = "image_api_key"
PREFIX = "image_"


def sub_config(config: Any, prefix: str = PREFIX) -> dict[str, Any]:
    """从扁平配置里摘出属于本层的那几条（按前缀），并剥掉前缀。

    为什么扁平：`config_schema()` 生成的是 `key = value` 列表，面板和 `haiq init`
    都按这个结构渲染。用前缀比嵌套表更省事，也不影响内核。
    """
    items = dict(config or {})
    sub = {k[len(prefix) :]: v for k, v in items.items() if k.startswith(prefix)}
    # 内核的演示模式对所有插件统一下发同一个 `provider` 键（它不该认识
    # image_/chat_ 这种前缀），所以这里让它生效。
    if "provider" not in sub and "provider" in items:
        sub["provider"] = items["provider"]
    return sub


def build_service(pctx: ProviderContext) -> Any:
    """组装生图服务。凭据只通过槽位取，provider 本身不接触密钥文本。"""
    from .broker import ImageBroker
    from .config import ImageCfg
    from .providers.mock import MockImageProvider
    from .providers.openai_compat import OpenAICompatProvider

    cfg = ImageCfg.model_validate(sub_config(pctx.config))
    if cfg.provider == "mock":
        provider: Any = MockImageProvider()
    else:
        provider = OpenAICompatProvider(pctx.slot(SLOT).headers, cfg)

    service = ImageBroker(provider, pctx.media, default_size=cfg.size, plugin_id=pctx.plugin_id)
    # 这三个属性是能力句柄会读的元信息（限额、可选尺寸、"我用的哪个模型"）
    service.max_prompt = cfg.max_prompt_chars
    service.sizes = set(IMAGE_SIZES)
    service.model = cfg.model
    return service
