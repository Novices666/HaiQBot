"""助手插件：一个入口，既能对话，也能按需出图，并带内容审查。

设计要点（都有代价，写在这里备查）：

1. **一个模型调用干三件事**：判断"要不要画图"、产出给群友的回话、产出给生图模型的
   描述。分开调三次是浪费钱和时间，而且三者的上下文本来就该一致。
2. **校验失败就降级成文字**，绝不在看不懂的模型返回上烧生图额度。
3. **尺寸不让模型算**：正则抽 → 吸附到合法档位 → 在回复里说明实际尺寸。
4. **不发 ack**：`ctx.reply.ack()` 等价于 `text()`，会永久多一条可见消息；官方通道
   还对同一 msg_id 限制 5 次回复，ack 会吃掉一个配额。宁可安静。
5. **审查分层**：词表（免费、确定）→ 模型（语义）。auto 模式下"问模型"是免费的，
   因为路由调用的返回里本来就带 violation 字段。
"""

from __future__ import annotations

import json
import re
from typing import Any

from haiqbot.plugin_api import (
    Context,
    Plugin,
    ProviderContext,
    Role,
    SecretSlot,
    ServiceSpec,
    on_command,
    on_message,
)

from . import chat as chat_layer
from . import image as image_layer
from . import moderation, router
from .util import safe_reason

DEFAULT_HISTORY_TURNS = 6

#: hybrid 模式下"明确要图"的硬规则。只在 router_mode=hybrid 时使用。
_DRAW_HINT = re.compile(r"(画|绘制|生成|来)(一|个|张|幅)?.*(图|画|插画|头像|壁纸|海报|表情包)")


class Assistant(Plugin):
    id = "assistant"
    name = "助手"
    description = "一个入口：对话 + 按需出图 + 内容审查（自带 image/chat 两套 provider）"

    #: kv 存多轮上下文与拦截计数；moderation 用于撤回/禁言。
    capabilities = ["kv", "moderation"]

    #: 两个服务都对外提供，契约版本与原 painter/chat 一致 —— 删插件不等于毁契约。
    provides = {
        "image:generate": ServiceSpec(
            name="image:generate",
            factory="build_image_service",
            version=1,
            methods=("generate",),
            doc="按提示词生成图片，返回 ImageRef",
        ),
        "llm:chat": ServiceSpec(
            name="llm:chat",
            factory="build_chat_service",
            version=1,
            methods=("chat",),
            doc="多轮对话，返回模型回复文本",
        ),
    }
    # 自己提供自己消费：自提供的服务自动视为已满足
    services = {"image:generate": "1", "llm:chat": "1"}

    secrets = {
        "image_api_key": SecretSlot(
            label="生图 API Key",
            ref="HAIQ_IMAGE_API_KEY",  # 绑已有键名，vault 零迁移
            description="OpenAI 兼容的 images/generations 密钥",
        ),
        "chat_api_key": SecretSlot(
            label="对话 API Key",
            ref="HAIQ_CHAT_API_KEY",
            description="OpenAI 兼容的 chat/completions 密钥",
        ),
    }

    @classmethod
    def config_schema(cls) -> list[dict[str, Any]]:
        return [
            {
                "key": "router_mode",
                "label": "路由方式",
                "kind": "str",
                "default": "auto",
                "choices": ["auto", "hybrid", "command"],
                "description": (
                    "auto=模型判断（会多一次模型调用）；"
                    "hybrid=只认明确说法（不额外花钱）；command=只有 /画图 才出图"
                ),
            },
            {
                "key": "image_threshold",
                "label": "出图置信度阈值",
                "kind": "float",
                "default": 0.8,
                "description": "模型判断要画图但把握低于这个值时不画，改为文字回应（误出图很贵）",
            },
            {
                "key": "clarify_text",
                "label": "拿不准时的提示",
                "kind": "str",
                "default": "要画图的话说「画一张……」，或者直接 /画图 <描述>。",
            },
            # -- 生图 --
            {
                "key": "image_provider",
                "label": "生图 provider",
                "kind": "str",
                "default": "openai_compat",
                "choices": ["openai_compat", "mock"],
                "description": "mock 不出网、返回占位图，用于调试",
            },
            {
                "key": "image_base_url",
                "label": "生图 API 基地址",
                "kind": "str",
                "default": "https://api.openai.com/v1",
            },
            {
                "key": "image_model",
                "label": "生图模型",
                "kind": "str",
                "default": "gpt-image-1",
                "choices": list(image_layer.IMAGE_MODELS),
            },
            {
                "key": "image_size",
                "label": "默认尺寸",
                "kind": "str",
                "default": "1024x1024",
                "choices": list(image_layer.IMAGE_SIZES),
                "description": "用户没指定尺寸时用这个；用户指定了会吸附到最接近的合法档位",
            },
            {
                "key": "image_timeout_seconds",
                "label": "生图超时(秒)",
                "kind": "float",
                "default": 180,
            },
            {
                "key": "image_max_prompt_chars",
                "label": "提示词上限",
                "kind": "int",
                "default": 800,
            },
            # -- 对话 --
            {
                "key": "chat_provider",
                "label": "对话 provider",
                "kind": "str",
                "default": "openai_compat",
                "choices": ["openai_compat", "mock"],
                "description": "mock 不出网，返回固定回答，用于调试",
            },
            {
                "key": "chat_base_url",
                "label": "对话 API 基地址",
                "kind": "str",
                "default": "https://api.openai.com/v1",
            },
            {
                "key": "chat_model",
                "label": "对话模型",
                "kind": "str",
                "default": "deepseek-v4.1-flash",
                "choices": list(chat_layer.CHAT_MODELS),
            },
            {
                "key": "chat_system_prompt",
                "label": "系统提示词（纯聊天时用）",
                "kind": "str",
                "default": "你是一个简洁友好的群聊助手，用中文回答，控制在 150 字以内。",
                "description": "router_mode=auto 时用的是路由自带的提示词，这里只影响 command/hybrid 的纯聊天",
            },
            {
                "key": "chat_max_tokens",
                "label": "最大回复 tokens",
                "kind": "int",
                "default": 800,
            },
            {"key": "chat_temperature", "label": "温度", "kind": "float", "default": 0.7},
            {"key": "chat_timeout_seconds", "label": "对话超时(秒)", "kind": "float", "default": 90},
            {
                "key": "chat_max_input_chars",
                "label": "输入上限（字）",
                "kind": "int",
                "default": 2000,
            },
            {
                "key": "chat_history_turns",
                "label": "上下文轮数",
                "kind": "int",
                "default": DEFAULT_HISTORY_TURNS,
                "description": "保留最近几轮（1 轮 = 一问一答）；0 = 不带上下文",
            },
            {"key": "chat_retries", "label": "网关重试次数", "kind": "int", "default": 4},
            {
                "key": "chat_retry_backoff_seconds",
                "label": "重试退避基数",
                "kind": "float",
                "default": 1.0,
            },
            # -- 审查 --
            {
                "key": "moderation_enabled",
                "label": "启用内容审查",
                "kind": "bool",
                "default": True,
            },
            {
                "key": "moderation_words",
                "label": "违禁词表（逗号分隔）",
                "kind": "str",
                "default": ",".join(moderation.DEFAULT_WORDS),
                "description": "命中即按「命中动作」处理。换成你自己群的规则。",
            },
            {
                "key": "mute_seconds",
                "label": "禁言时长(秒)",
                "kind": "int",
                "default": 600,
                "description": "命中违规后禁言多久；0 = 只警告不禁言",
            },
            {
                "key": "recall_on_hit",
                "label": "同时撤回违规消息",
                "kind": "bool",
                "default": False,
            },
            {
                "key": "exempt_admins",
                "label": "管理员/群主免审",
                "kind": "bool",
                "default": True,
            },
            {
                "key": "moderate_prompt",
                "label": "出图前再问一次模型",
                "kind": "bool",
                "default": True,
                "description": "词表没命中时，用模型再判一次生图描述（要烧钱了，值得多一次便宜调用）",
            },
            {
                "key": "moderate_input_model",
                "label": "输入审查用模型（仅非 auto 模式）",
                "kind": "bool",
                "default": False,
                "description": "auto 模式下路由返回里已带违规标记，不用再调；其他模式开启会让每条消息多一次调用",
            },
        ]

    # -- 能力提供 --------------------------------------------------------- #

    def build_image_service(self, pctx: ProviderContext) -> Any:
        return image_layer.build_service(pctx)

    def build_chat_service(self, pctx: ProviderContext) -> Any:
        return chat_layer.build_service(pctx)

    # -- 入口 ------------------------------------------------------------- #

    @on_command("问", aliases=["ask", "ai"], usage="问 <问题>", priority=15, timeout=180)
    async def ask(self, ctx: Context, question: str = "") -> None:
        await self._handle(ctx, question, use_history=False)

    @on_message(require_at=True, priority=-30, block=True, timeout=180)
    async def talk(self, ctx: Context) -> None:
        text = ctx.event.text.strip() if ctx.event else ""
        if not text or text.startswith("/"):
            return
        await self._handle(ctx, text, use_history=True)

    @on_command("画图", aliases=["draw", "画画"], usage="画图 <描述>", priority=20, timeout=180)
    async def draw(self, ctx: Context, prompt: str = "") -> None:
        """强制出图：不过路由、不问模型，只做尺寸解析与审查。"""
        text = (prompt or "").strip()
        if not text:
            await ctx.reply.text("用法：画图 <描述>   例如：画图 一只在雪地里的橘猫 16:9")
            return

        if await self._blocked_by_lexicon(ctx, text):
            return

        prepared = self._prepare_prompt(text, hint=None, default_size=self._default_size(ctx))
        if prepared is None:
            return
        prompt_text, size, size_note = prepared
        if await self._review_prompt(ctx, prompt_text):
            return
        await self._generate_and_send(ctx, prompt_text, size, size_note=size_note)

    @on_command("重置对话", aliases=["reset"], usage="重置对话", priority=15, timeout=30)
    async def reset(self, ctx: Context) -> None:
        if ctx.kv is None:
            await ctx.reply.text("kv 能力未启用，无法重置上下文")
            return
        await ctx.kv.delete(self._history_key(ctx))
        await ctx.reply.text("上下文已清空")

    # -- 主流程 ----------------------------------------------------------- #

    async def _handle(self, ctx: Context, raw: str, *, use_history: bool) -> None:
        text = (raw or "").strip()
        if not text:
            await ctx.reply.text("用法：/问 <问题>，或者直接 @我 说点什么；想要图就 /画图 <描述>")
            return

        if await self._blocked_by_lexicon(ctx, text):
            return

        mode = str(ctx.config.get("router_mode", "auto")).strip().lower()
        if mode == "command":
            await self._plain_chat(ctx, text, use_history=use_history)
            return
        if mode == "hybrid":
            await self._hybrid(ctx, text, use_history=use_history)
            return
        await self._auto(ctx, text, use_history=use_history)

    async def _plain_chat(self, ctx: Context, text: str, *, use_history: bool) -> None:
        """纯聊天：不做意图判断，也不审查模型输出（除词表）。"""
        llm = ctx.service("llm:chat")
        messages: Any = text
        if use_history:
            history = await self._history(ctx)
            if history:
                messages = [*history, {"role": "user", "content": text}]
        try:
            answer = await llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("assistant_chat_failed", error_type=type(exc).__name__)
            await ctx.reply.text(f"❌ 对话失败：{safe_reason(exc)}")
            return
        if await self._blocked_output(ctx, answer):
            return
        if use_history:
            await self._remember(ctx, text, answer)
        await ctx.reply.text(answer)

    async def _hybrid(self, ctx: Context, text: str, *, use_history: bool) -> None:
        """hybrid：硬规则命中才出图，否则纯聊天。不花额外的判断调用。"""
        if self._looks_like_draw_request(text):
            prepared = self._prepare_prompt(
                text,
                hint=None,
                strip_draw_words=True,
                default_size=self._default_size(ctx),
            )
            if prepared is None:
                return
            prompt_text, size, size_note = prepared
            if await self._review_prompt(ctx, prompt_text):
                return
            await self._generate_and_send(ctx, prompt_text, size, size_note=size_note)
            return
        await self._plain_chat(ctx, text, use_history=use_history)

    async def _auto(self, ctx: Context, text: str, *, use_history: bool) -> None:
        """auto：一次模型调用决定「聊天还是画图」，并顺带拿到违规标记。"""
        llm = ctx.service("llm:chat")
        history = await self._history(ctx) if use_history else []
        messages = router.build_messages(text, history)

        try:
            raw = await llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("assistant_route_failed", error_type=type(exc).__name__)
            await ctx.reply.text(f"❌ 对话失败：{safe_reason(exc)}")
            return

        decision = router.parse_decision(raw)
        if decision is None:
            # 模型没按格式返回：当纯文本回答处理，绝不出图
            ctx.log.warning("assistant_decision_unparsed", chars=len(raw or ""))
            answer = (raw or "").strip() or "（模型没有返回内容）"
            if await self._blocked_output(ctx, answer):
                return
            if use_history:
                await self._remember(ctx, text, answer)
            await ctx.reply.text(answer)
            return

        ctx.log.info(
            "assistant_decided",
            action=decision.action,
            confidence=round(decision.confidence, 2),
            size_hint=decision.size_hint or "",
            why=decision.why,
        )

        # 违规标记来自同一次调用：不发图、不聊天，直接处理
        if decision.violation is not None:
            await self._punish(ctx, decision.violation.category, decision.violation.why)
            return

        threshold = self._float_cfg(ctx, "image_threshold", 0.8)
        if decision.wants_image and decision.confidence >= threshold:
            prepared = self._prepare_prompt(
                text,
                hint=decision.size_hint,
                prompt=decision.prompt,
                default_size=self._default_size(ctx),
            )
            if prepared is None:
                return
            prompt_text, size, size_note = prepared
            if await self._review_prompt(ctx, prompt_text):
                return
            if decision.reply:
                await ctx.reply.text(decision.reply)  # 出图要 30s+，先说一句
            await self._generate_and_send(ctx, prompt_text, size, size_note=size_note)
            return

        if decision.wants_image and decision.confidence < threshold:
            # 说要画但没把握：回文字 + 提示怎么强制
            clarify = str(ctx.config.get("clarify_text") or "").strip()
            reply = decision.reply or ""
            message = f"{reply}\n{clarify}".strip() if clarify else reply
            await ctx.reply.text(message or clarify or "没太听明白，能再说一次吗？")
            return

        answer = decision.reply or "（没有生成回复）"
        if await self._blocked_output(ctx, answer):
            return
        if use_history:
            await self._remember(ctx, text, answer)
        await ctx.reply.text(answer)

    # -- 生图 ------------------------------------------------------------- #

    def _prepare_prompt(
        self,
        original: str,
        *,
        hint: str | None,
        prompt: str | None = None,
        strip_draw_words: bool = False,
        default_size: str = router.DEFAULT_SIZE,
    ) -> tuple[str, str, str] | None:
        """把「用户原句 / 模型给的描述」整理成 (prompt, size, 尺寸说明)。

        尺寸优先取模型给的 hint，其次从原句里抽 —— 但**始终用正则归一化**，
        因为模型算比例会幻觉，而这个数字直接决定请求体的 size 字段。
        """
        size_key = ""
        note = ""
        for candidate in (hint, original):
            parsed = router.parse_size(candidate or "")
            if parsed is None:
                continue
            size_key = router.absorb_size(parsed.ratio)
            actual = router.describe_size(size_key)
            if parsed.label and parsed.label.replace("x", "×") != actual:
                note = f"（实际 {actual}）"
            break
        if not size_key:
            size_key = (
                default_size
                if default_size in router.SIZES or default_size == "auto"
                else router.DEFAULT_SIZE
            )

        body = (prompt or original or "").strip()
        # 只剥「这段文字自己」里的尺寸：拿 A 的 span 去切 B 的字符串会切错位置
        parsed_body = router.parse_size(body)
        if parsed_body is not None:
            body = router.strip_spans(body, parsed_body.spans)
        if strip_draw_words:
            body = re.sub(r"^(请|帮我|帮忙|给我|能不能)?\s*(画|绘制|生成|来)", "", body).strip(" ：:，,")
        if not body:
            return None
        return body, size_key, note

    async def _generate_and_send(self, ctx: Context, prompt: str, size: str, *, size_note: str = "") -> None:
        image = ctx.service("image:generate")
        if ctx.reply.almost_expired(margin=30.0):
            await ctx.reply.text("这条消息快过期了，重新 @我 一次吧")
            return

        try:
            ref = await image.generate(prompt, size=size)
        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("assistant_image_failed", error_type=type(exc).__name__)
            await ctx.reply.text(f"❌ 生成失败：{safe_reason(exc)}")
            return

        if ctx.reply.almost_expired(margin=5.0):
            ctx.log.warning("assistant_window_closed", media_id=ref.media_id)
            await ctx.reply.text(f"出图太慢，窗口已关闭（图已存在媒体库，ID={ref.media_id}）")
            return

        try:
            await ctx.reply.image(ref)
        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("assistant_send_failed", error_type=type(exc).__name__, media_id=ref.media_id)
            await ctx.reply.text(
                f"❌ 发送失败：图片已生成（媒体库 ID={ref.media_id}），但发到群里失败：{safe_reason(exc)}"
            )
            return
        if size_note:
            ctx.log.info("assistant_image_sent", media_id=ref.media_id, size=size, note=size_note)

    # -- 审查 ------------------------------------------------------------- #

    def _exempt(self, ctx: Context) -> bool:
        if not bool(ctx.config.get("exempt_admins", True)):
            return False
        role = ctx.event.actor.role if ctx.event else None
        return role in (Role.ADMIN, Role.OWNER)

    def _words(self, ctx: Context) -> tuple[str, ...]:
        raw = ctx.config.get("moderation_words", "")
        words = moderation.words_from_config(raw)
        return words or moderation.DEFAULT_WORDS

    async def _blocked_by_lexicon(self, ctx: Context, text: str) -> bool:
        """词表拦截（免费）。命中就禁言并返回 True —— 调用方直接结束。"""
        if not bool(ctx.config.get("moderation_enabled", True)) or self._exempt(ctx):
            return False
        hit = moderation.lexicon_hit(text, self._words(ctx), moderation.DEFAULT_PATTERNS)
        if hit is None:
            return False
        await self._punish(ctx, hit.category, hit.evidence)
        return True

    async def _review_prompt(self, ctx: Context, prompt: str) -> bool:
        """出图前再审一次描述。命中返回 True（调用方不出图）。"""
        if not bool(ctx.config.get("moderation_enabled", True)):
            return False
        if moderation.lexicon_hit(prompt, self._words(ctx), moderation.DEFAULT_PATTERNS) is not None:
            await ctx.reply.text("🙅 这个描述不适合出图，换个说法吧")
            return True
        if not bool(ctx.config.get("moderate_prompt", True)):
            return False
        try:
            raw = await ctx.service("llm:chat").chat(moderation.build_check_messages(prompt))
        except Exception as exc:  # noqa: BLE001
            ctx.log.warning("assistant_prompt_review_failed", error_type=type(exc).__name__)
            return False  # 审查挂了不该挡住正常出图
        hit = moderation.parse_check(raw)
        if hit is None:
            return False
        ctx.log.info("assistant_prompt_blocked", category=hit.category)
        await ctx.reply.text("🙅 这个描述不适合出图，换个说法吧")
        return True

    async def _blocked_output(self, ctx: Context, text: str) -> bool:
        """模型输出审查（默认只过词表：让模型审自己说的话并不可靠）。"""
        if not bool(ctx.config.get("moderation_enabled", True)):
            return False
        hit = moderation.lexicon_hit(text, self._words(ctx), moderation.DEFAULT_PATTERNS)
        if hit is None:
            return False
        ctx.log.warning("assistant_output_blocked", category=hit.category)
        await ctx.reply.text("🙅 这条回答不合适，已拦截。换个问法试试？")
        return True

    async def _punish(self, ctx: Context, category: str, evidence: str) -> None:
        """命中违规：按配置禁言（默认 600 秒）、可选撤回，并回一句说明。"""
        seconds = int(self._float_cfg(ctx, "mute_seconds", 600))
        actor = ctx.event.actor.id if ctx.event else ""
        channel = ctx.event.channel if ctx.event else None

        if bool(ctx.config.get("recall_on_hit", False)) and channel is not None:
            msg_id = str(getattr(ctx.event, "msg_id", "") or "")
            if msg_id:
                try:
                    await ctx.moderation.recall(channel, msg_id)
                except Exception as exc:  # noqa: BLE001
                    ctx.log.warning("assistant_recall_failed", error_type=type(exc).__name__)

        muted = False
        if seconds > 0 and channel is not None and actor:
            try:
                muted = await ctx.moderation.mute(channel, actor, seconds)
            except Exception as exc:  # noqa: BLE001
                ctx.log.warning("assistant_mute_failed", error_type=type(exc).__name__)

        ctx.log.info("assistant_punished", category=category, muted=muted, seconds=seconds)
        if muted:
            minutes = max(1, seconds // 60)
            await ctx.reply.text(f"🚫 已拦截（{category}），并禁言 {minutes} 分钟。")
        elif seconds > 0:
            await ctx.reply.text(f"🚫 已拦截（{category}）。禁言没生效，可能缺少管理权限。")
        else:
            await ctx.reply.text(f"🚫 已拦截（{category}），请注意言辞。")

    # -- 配置读取 --------------------------------------------------------- #

    @staticmethod
    def _float_cfg(ctx: Context, key: str, default: float) -> float:
        try:
            return float(ctx.config.get(key, default))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _default_size(ctx: Context) -> str:
        """配置里的默认档位（校验交给 _prepare_prompt）。"""
        raw = str(ctx.config.get("image_size") or "").strip()
        return raw or router.DEFAULT_SIZE

    @staticmethod
    def _looks_like_draw_request(text: str) -> bool:
        return bool(_DRAW_HINT.search(text))

    # -- 上下文 ----------------------------------------------------------- #

    @staticmethod
    def _history_key(ctx: Context) -> str:
        channel = ctx.event.channel if ctx.event else "unknown"
        actor = ctx.event.actor.id if ctx.event else "unknown"
        return f"assistant:hist:{channel}:{actor}"

    def _history_limit(self, ctx: Context) -> int:
        raw = ctx.config.get("chat_history_turns", DEFAULT_HISTORY_TURNS)
        try:
            turns = max(0, int(raw))
        except (TypeError, ValueError):
            turns = DEFAULT_HISTORY_TURNS
        return turns * 2

    async def _history(self, ctx: Context) -> list[dict[str, str]]:
        if ctx.kv is None:
            return []
        try:
            raw = await ctx.kv.get(self._history_key(ctx), "[]")
            history = json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(history, list):
            return []
        cleaned = [
            {"role": m["role"], "content": m["content"]}
            for m in history
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
        ]
        limit = self._history_limit(ctx)
        return cleaned[-limit:] if limit else []

    async def _remember(self, ctx: Context, question: str, answer: str) -> None:
        if ctx.kv is None:
            return
        key = self._history_key(ctx)
        try:
            raw = await ctx.kv.get(key, "[]")
            history = json.loads(raw) if raw else []
            if not isinstance(history, list):
                history = []
        except Exception:  # noqa: BLE001
            history = []
        history.append({"role": "user", "content": question})
        history.append({"role": "assistant", "content": answer})
        limit = self._history_limit(ctx)
        await ctx.kv.set(key, json.dumps(history[-limit:] if limit else [], ensure_ascii=False))
