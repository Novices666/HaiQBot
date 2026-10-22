"""内置插件：生图。



这个插件**自带**生图实现，并用 `ServiceSpec` 对外声明服务：

内核不认识"生图"，只保管能力表（服务名 → 实现 + 契约）。



    拔掉 painter → `image:generate` 从表里消失 → 依赖它的插件进入 blocked（暂停）



参数全部在 `[plugins.painter]`；内核没有 `[image]` 段。



同时它示范了三件事：

1. 能力协商：平台不支持图片就降级，而不是崩；

2. 两枪回复：先 ack 占住交互，再补图；

3. 窗口兜底：剩余时间不够就不调用生图（省额度）。

"""

from __future__ import annotations

import re
from typing import Any

from haiqbot.plugin_api import (
    Context,
    Plugin,
    ProviderContext,
    SecretSlot,
    ServiceSpec,
    on_command,
)

IMAGE_SIZES = ("1024x1024", "1536x1024", "1024x1536", "512x512", "auto")

IMAGE_MODELS = ("gpt-image-1", "gpt-image-2", "gpt-image-2.5", "dall-e-3")


_SECRET_LIKE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+|(?:api[_-]?key|token|secret)=[^\s&]+)", re.I)


def _safe_reason(exc: BaseException) -> str:
    """把异常压成一行人话：类型 + 概要，并掩掉任何像凭据的片段。

    群里要能看出"哪一步失败、为什么"，但绝不能把 key 带出去。
    """
    text = _SECRET_LIKE.sub("<redacted>", str(exc) or "")
    text = " ".join(text.split())
    if not text:
        return type(exc).__name__
    return f"{type(exc).__name__}: {text[:120]}"


class Painter(Plugin):
    id = "painter"

    name = "生图"

    description = "根据提示词生成图片（自带 provider）"

    # 内核不再有 image 能力：服务名、版本、方法都由插件自己声明

    provides = {
        "image:generate": ServiceSpec(
            name="image:generate",
            factory="build_image_service",
            version=1,
            methods=("generate",),
            doc="按提示词生成图片，返回 ImageRef",
        )
    }

    # 自己既是提供方又是消费方：自提供的服务自动视为已满足

    services = {"image:generate": "1"}

    @classmethod
    def config_schema(cls) -> list[dict[str, Any]]:

        return [
            {
                "key": "provider",
                "label": "生图 provider",
                "kind": "str",
                "default": "openai_compat",
                "choices": ["openai_compat", "mock"],
                "description": "mock 不出网、返回占位图，用于调试",
            },
            {
                "key": "base_url",
                "label": "API 基地址",
                "kind": "str",
                "default": "https://api.openai.com/v1",
            },
            {
                "key": "model",
                "label": "生图模型",
                "kind": "str",
                "default": "gpt-image-1",
                "choices": list(IMAGE_MODELS),
            },
            {
                "key": "api_key_ref",
                "label": "密钥键名（可留空）",
                "kind": "str",
                "default": "WEFT_IMAGE_API_KEY",
                "description": "只填 vault 里的键名，不要粘贴密钥本身；留空则用默认键名",
            },
            {
                "key": "size",
                "label": "默认尺寸",
                "kind": "str",
                "default": "1024x1024",
                "choices": list(IMAGE_SIZES),
            },
            {"key": "timeout_seconds", "label": "超时(秒)", "kind": "float", "default": 180},
            {"key": "max_prompt_chars", "label": "提示词上限", "kind": "int", "default": 800},
            {
                "key": "ack_text",
                "label": "排队回执文案",
                "kind": "str",
                "default": "🎨 排队中…",
                "description": "生图要 30s+，先回这句占住交互。留空 = 不发回执。",
            },
        ]

    # -- 能力提供 --------------------------------------------------------- #

    secrets = {
        "api_key": SecretSlot(
            label="生图 API Key",
            ref="WEFT_IMAGE_API_KEY",  # 绑已有键名，vault 零迁移
            description="OpenAI 兼容的 images/generations 密钥",
        ),
    }

    def build_image_service(self, pctx: ProviderContext) -> Any:
        """构造生图服务。凭据通过槽位 `api_key` 取，provider 不接触密钥。"""

        from .broker import ImageBroker
        from .config import ImageCfg
        from .providers.mock import MockImageProvider
        from .providers.openai_compat import OpenAICompatProvider

        cfg = ImageCfg.model_validate(dict(pctx.config))

        if cfg.provider == "mock":
            provider: Any = MockImageProvider()

        else:
            provider = OpenAICompatProvider(pctx.slot("api_key").headers, cfg)

        service = ImageBroker(provider, pctx.media, default_size=cfg.size, plugin_id=pctx.plugin_id)

        # 能力句柄会读这三个属性（限额与"我到底用的哪个模型"都归服务声明）

        service.max_prompt = cfg.max_prompt_chars

        service.sizes = {cfg.size}

        service.model = cfg.model

        return service

    # -- 入口 ------------------------------------------------------------- #

    @on_command("画图", aliases=["draw", "画画"], usage="画图 <描述>", priority=20, timeout=180)
    async def draw(self, ctx: Context, prompt: str = "") -> None:

        text = prompt.strip()

        if not text:
            await ctx.reply.text("用法：画图 <描述>  例如：画图 一只在雪地里的橘猫")

            return

        image = ctx.service("image:generate")

        if ctx.reply.almost_expired(margin=30.0):
            await ctx.reply.text("这条消息快过期了，重新 @我 一次吧")

            return

        ack_text = str(ctx.config.get("ack_text", "🎨 排队中…")).strip()

        if ack_text:
            await ctx.reply.ack(ack_text)

        try:
            ref = await image.generate(text)

        except Exception as exc:  # noqa: BLE001
            # 只记类型，不记 message —— 避免任何可能的敏感串外泄

            ctx.log.exception("painter_failed", error_type=type(exc).__name__)

            await ctx.reply.text(f"❌ 生成失败：{_safe_reason(exc)}")

            return

        if ctx.reply.almost_expired(margin=5.0):
            ctx.log.warning("painter_window_closed", media_id=ref.media_id)

            await ctx.reply.text(f"出图太慢，窗口已关闭（图已存在媒体库，ID={ref.media_id}）")

            return

        # 图已生成 -> 发送是**另一个阶段**，失败必须分开报，否则排障时看不出来
        try:
            await ctx.reply.image(ref)

        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("painter_send_failed", error_type=type(exc).__name__, media_id=ref.media_id)

            await ctx.reply.text(
                f"❌ 发送失败：图片已生成（媒体库 ID={ref.media_id}），但发到群里失败：{_safe_reason(exc)}"
            )

            return
