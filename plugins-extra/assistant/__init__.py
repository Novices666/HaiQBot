"""助手插件：一个入口，既能对话，也能按需出图，并带内容审查。

设计要点（都有代价，写在这里备查）：

1. **一个模型调用干三件事**：判断"要不要画图"、产出给群友的回话、产出给生图模型的
   描述。分开调三次是浪费钱和时间，而且三者的上下文本来就该一致。
2. **校验失败就降级成文字**，绝不在看不懂的模型返回上烧生图额度。
3. **尺寸由模型判断、代码只做安全吸附**：模型给 `size_hint`（比例 / 分辨率 /
   方向词），代码用 `absorb_size` 吸附到接口认得的档位（绝不透传，透传会 400）；
   模型没给时用正则兜底。尺寸只写日志，**不进回复**——回复里念参数很生硬。
4. **不发 ack**：`ctx.reply.ack()` 等价于 `text()`，会永久多一条可见消息；官方通道
   还对同一 msg_id 限制 5 次回复，ack 会吃掉一个配额。宁可安静。
5. **审查分层**：词表（免费、确定）→ 模型（语义）。auto 模式下"问模型"是免费的，
   因为路由调用的返回里本来就带 violation 字段。
"""

from __future__ import annotations

import json
import re
import time
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
from .util import safe_detail

DEFAULT_HISTORY_TURNS = 6

#: hybrid 模式下"明确要图"的硬规则。只在 router_mode=hybrid 时使用。
_DRAW_HINT = re.compile(r"(画|绘制|生成|来)(一|个|张|幅)?.*(图|画|插画|头像|壁纸|海报|表情包)")

#: 发言人昵称会进 user 消息 —— 它是用户可控输入，所以截断 + 去掉能伪造对话结构的字符。
_NAME_MAX = 24
_NAME_TRANS = str.maketrans({"\n": " ", "\r": " ", "\t": " ", ":": " ", "：": " ", "|": " "})


def sender_label(ctx: Any) -> str:
    """从事件里取「谁在说话」：群名片优先，其次昵称。取不到就空串。"""
    event = getattr(ctx, "event", None)
    actor = getattr(event, "actor", None)
    raw = str(getattr(actor, "name", "") or "").strip()
    if not raw:
        return ""
    return " ".join(raw.translate(_NAME_TRANS).split())[:_NAME_MAX]


# --------------------------------------------------------------------------- #
# 统计：面板页用。按天存一条 kv 记录，重启不丢，跨天自动归零。
# --------------------------------------------------------------------------- #

_STATS_KEY = "stats:daily"


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _blank_stats() -> dict[str, Any]:
    return {
        "date": _today(),
        "draw_ok": 0,
        "draw_fail": 0,
        "ask_ok": 0,
        "ask_fail": 0,
        "blocked": 0,
        "users": {},
    }


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
                "key": "include_sender_name",
                "label": "告诉模型谁在说话",
                "kind": "bool",
                "default": True,
                "description": "把群昵称当作「昵称: 内容」前缀传给模型。昵称会被截断并去掉冒号/换行，"
                "避免被拿来伪造对话结构。",
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

    @on_command(
        "问",
        aliases=["ask", "ai"],
        usage="问 <问题>",
        description="单轮问答，不带上下文（要连续对话就用 @我）",
        priority=15,
        timeout=180,
        feature="ask",
    )
    async def ask(self, ctx: Context, question: str = "") -> None:
        await self._handle(ctx, question, use_history=False)

    @on_message(require_at=True, priority=-30, block=True, timeout=180, feature="ask")
    async def talk(self, ctx: Context) -> None:
        text = ctx.event.text.strip() if ctx.event else ""
        if not text or text.startswith("/"):
            return
        await self._handle(ctx, text, use_history=True)

    @on_command(
        "画图",
        aliases=["draw", "画画"],
        usage="画图 <描述>",
        description="强制出图，支持「16:9」「1920x1080」这类尺寸",
        priority=20,
        timeout=180,
        feature="draw",
    )
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

    @on_command(
        "重置对话",
        aliases=["reset"],
        usage="重置对话",
        description="清掉本群你和我的对话上下文",
        priority=15,
        timeout=30,
        feature="reset",
    )
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

        # 给模型看的内容带上「谁在说话」，但审查用原始文本（昵称不该触发违禁词判定）
        spoken = self._named(ctx, text)

        mode = str(ctx.config.get("router_mode", "auto")).strip().lower()
        if mode == "command":
            await self._plain_chat(ctx, spoken, use_history=use_history)
            return
        if mode == "hybrid":
            await self._hybrid(ctx, spoken, use_history=use_history)
            return
        await self._auto(ctx, spoken, use_history=use_history)

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
            await self._bump(ctx, "ask_fail", actor=self._actor(ctx))
            await ctx.reply.text(f"欸，我卡住了…（对话失败：{safe_detail(exc)}）")
            return
        if await self._blocked_output(ctx, answer):
            return
        if use_history:
            await self._remember(ctx, text, answer)
        await ctx.reply.text(answer)
        await self._bump(ctx, "ask_ok", actor=self._actor(ctx), kind="ask")

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
            await self._bump(ctx, "ask_fail", actor=self._actor(ctx))
            await ctx.reply.text(f"欸，我卡住了…（对话失败：{safe_detail(exc)}）")
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
            await self._bump(ctx, "ask_ok", actor=self._actor(ctx), kind="ask")
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
            await self._bump(ctx, "ask_ok", actor=self._actor(ctx), kind="ask")
            return

        answer = decision.reply or "（没有生成回复）"
        if await self._blocked_output(ctx, answer):
            return
        if use_history:
            await self._remember(ctx, text, answer)
        await ctx.reply.text(answer)
        await self._bump(ctx, "ask_ok", actor=self._actor(ctx), kind="ask")

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
        # 优先级统一收在 router.decide_size 里（原句 > 模型方向词 > 默认档）
        size_key, parsed_size = router.decide_size(original, hint, default=default_size)
        note = ""
        if parsed_size is not None:
            actual = router.describe_size(size_key)
            if parsed_size.label and parsed_size.label.replace("x", "×") != actual:
                note = f"（实际 {actual}）"

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
            await self._bump(ctx, "draw_fail", actor=self._actor(ctx))
            await ctx.reply.text(f"呜，画不出来…（生成失败：{safe_detail(exc)}）")
            return

        if ctx.reply.almost_expired(margin=5.0):
            ctx.log.warning("assistant_window_closed", media_id=ref.media_id)
            await self._bump(ctx, "draw_fail", actor=self._actor(ctx))
            await ctx.reply.text(f"出图太慢啦，消息窗口已经关了…图存下来了（媒体库 ID={ref.media_id}）")
            return

        try:
            await ctx.reply.image(ref)
        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("assistant_send_failed", error_type=type(exc).__name__, media_id=ref.media_id)
            await self._bump(ctx, "draw_fail", actor=self._actor(ctx))
            await ctx.reply.text(
                f"图画好了，可就是送不出去…\n（发送失败：{safe_detail(exc)}；媒体库 ID={ref.media_id}）"
            )
            return
        if size_note:
            ctx.log.info("assistant_image_sent", media_id=ref.media_id, size=size, note=size_note)
        await self._bump(ctx, "draw_ok", actor=self._actor(ctx), kind="draw")

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
        await self._bump(ctx, "blocked", actor=self._actor(ctx))
        await ctx.reply.text("🙅 这条回答不合适，已拦截。换个问法试试？")
        return True

    async def _punish(self, ctx: Context, category: str, evidence: str) -> None:
        """命中违规：按配置禁言（默认 600 秒）、可选撤回，并回一句说明。"""
        seconds = int(self._float_cfg(ctx, "mute_seconds", 600))
        actor = ctx.event.actor.id if ctx.event else ""
        channel = ctx.event.channel if ctx.event else None
        await self._bump(ctx, "blocked", actor=self._actor(ctx))

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
    def _flag(ctx: Context, key: str, default: bool) -> bool:
        """配置里的开关：TOML 给的是真 bool，但面板/旧文件可能存成字符串。"""
        raw = ctx.config.get(key, default)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)

    def _named(self, ctx: Context, text: str) -> str:
        """给模型看的文本加「昵称: 」前缀（可关）。历史里也存这个形态，多人对话才分得清谁说的。"""
        if not self._flag(ctx, "include_sender_name", True):
            return text
        label = sender_label(ctx)
        return f"{label}: {text}" if label else text

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

    # -- 统计与面板 ------------------------------------------------------- #

    def _actor(self, ctx: Context) -> str:
        event = getattr(ctx, "event", None)
        return str(getattr(getattr(event, "actor", None), "id", "") or "")

    @property
    def _stats(self) -> dict[str, Any]:
        """今日统计。跨天自动归零；首次访问时初始化。"""
        state = getattr(self, "_stats_state", None)
        if not isinstance(state, dict) or state.get("date") != _today():
            state = _blank_stats()
            self._stats_state = state
        return state

    async def on_load(self, ctx: Context) -> None:
        """把今天已落盘的统计读回来 —— 重启不该把「今日」清零。"""
        self._kv_ctx = ctx  # panel_action 拿不到 ctx，这里留个句柄用于清零落盘
        if ctx.kv is None:
            return
        try:
            raw = await ctx.kv.get(_STATS_KEY, "")
            saved = json.loads(raw) if raw else None
        except Exception as exc:  # noqa: BLE001
            ctx.log.warning("assistant_stats_load_failed", error_type=type(exc).__name__)
            return
        if not isinstance(saved, dict) or saved.get("date") != _today():
            return  # 昨天的记录：不读，等下一次写入时覆盖
        merged = _blank_stats()
        merged.update({k: v for k, v in saved.items() if k in merged})
        if not isinstance(merged.get("users"), dict):
            merged["users"] = {}
        self._stats_state = merged

    async def _bump(self, ctx: Context, field: str, *, actor: str = "", kind: str = "") -> None:
        """记一次。先写内存再写穿 kv；kv 出错绝不能影响主流程。"""
        stats = self._stats
        stats[field] = int(stats.get(field, 0)) + 1
        if actor and kind:
            users = stats.get("users")
            if not isinstance(users, dict):
                users = {}
                stats["users"] = users
            row = users.get(actor)
            if not isinstance(row, dict):
                row = {"ask": 0, "draw": 0}
                users[actor] = row
            row[kind] = int(row.get(kind, 0)) + 1
        await self._save_stats(ctx)

    async def _save_stats(self, ctx: Context) -> None:
        if ctx.kv is None:
            return
        try:
            await ctx.kv.set(_STATS_KEY, json.dumps(self._stats, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            ctx.log.warning("assistant_stats_save_failed", error_type=type(exc).__name__)

    def panel_meta(self) -> dict[str, Any]:
        return {"title": "助手 · 今日用量", "order": 20}

    async def panel_page(self, panel: Any) -> dict[str, Any]:
        stats = self._stats
        cfg = getattr(panel, "config", None) or {}
        rows = [
            {
                "user": str(uid),
                "draw": int(row.get("draw", 0)),
                "ask": int(row.get("ask", 0)),
            }
            for uid, row in (stats.get("users") or {}).items()
            if isinstance(row, dict)
        ]
        rows.sort(key=lambda r: (-r["draw"], -r["ask"], r["user"]))
        top = [{**r, "total": r["draw"] + r["ask"]} for r in rows[:10]]
        return {
            "title": "助手",
            "sections": [
                {
                    "type": "text",
                    "text": f"统计日期 {stats['date']}（本地时区；重启不丢，跨天自动归零）。"
                    "出图的「成功」与「失败」分开记：失败可能是模型没出图，"
                    "也可能是图出来了但发不进群 —— 后者看审计里的 assistant_send_failed。",
                },
                {
                    "type": "field_group",
                    "title": "今日",
                    "fields": [
                        {"label": "出图成功", "value": stats["draw_ok"]},
                        {"label": "出图失败", "value": stats["draw_fail"]},
                        {"label": "对话回复", "value": stats["ask_ok"]},
                        {"label": "对话失败", "value": stats["ask_fail"]},
                        {"label": "审查命中", "value": stats["blocked"]},
                        {"label": "路由方式", "value": str(cfg.get("router_mode", "auto"))},
                    ],
                },
                {
                    "type": "readonly_table",
                    "title": "Top 用户（按出图次数）",
                    "columns": [
                        {"key": "user", "title": "用户"},
                        {"key": "draw", "title": "出图"},
                        {"key": "ask", "title": "对话"},
                        {"key": "total", "title": "合计"},
                    ],
                    "rows": top or [{"user": "（今天还没有人用）", "draw": 0, "ask": 0, "total": 0}],
                },
                {
                    "type": "action_button",
                    "label": "清零今日统计",
                    "action": "reset",
                    "confirm": "清零今天的统计？",
                },
            ],
        }

    async def panel_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action != "reset":
            raise KeyError(action)
        self._stats_state = _blank_stats()
        ctx = getattr(self, "_kv_ctx", None)
        if ctx is not None:
            await self._save_stats(ctx)
        return {"message": "今日统计已清零"}
