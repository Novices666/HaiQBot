"""内置插件：AI 对话。

和 painter 一样，这个插件**自带**对话实现（provider），通过 `provides` 注册成能力，
再自己声明 `capabilities` 去消费它。参数全部在 `[plugins.chat]`。

两个入口：
- `/问 <问题>`：一次性问答，不带上下文
- `@机器人 <文本>`：带多轮上下文（按 频道+用户 隔离，存在 kv）

LLM 延迟通常 10s+，所以先 ack 再补答案，并且声明了 150s 的 handler 超时。
"""

from __future__ import annotations

import json
from typing import Any

from haiqbot.plugin_api import (
    Context,
    Plugin,
    ProviderContext,
    SecretSlot,
    ServiceSpec,
    on_command,
    on_message,
)

CHAT_MODELS = ("deepseek-v4.1-flash", "deepseek-v4-flash", "deepseek-v4-pro", "gpt-4o-mini")
DEFAULT_HISTORY_TURNS = 6


class Chat(Plugin):
    id = "chat"
    name = "AI 对话"
    description = "与 AI 对话（@机器人 或 /问，自带 provider）"
    # kv 是内核基础设施；llm:chat 是**本插件自己声明并提供的服务**
    capabilities = ["kv"]
    provides = {
        "llm:chat": ServiceSpec(
            name="llm:chat",
            factory="build_chat_service",
            version=1,
            methods=("chat",),
            doc="多轮对话，返回模型回复文本",
        )
    }
    # 自己提供自己消费：自提供的服务自动视为已满足
    services = {"llm:chat": "1"}

    @classmethod
    def config_schema(cls) -> list[dict[str, Any]]:
        return [
            {
                "key": "provider",
                "label": "对话 provider",
                "kind": "str",
                "default": "openai_compat",
                "choices": ["openai_compat", "mock"],
                "description": "mock 不出网，返回固定回答，用于调试",
            },
            {
                "key": "base_url",
                "label": "API 基地址",
                "kind": "str",
                "default": "https://api.openai.com/v1",
            },
            {
                "key": "model",
                "label": "对话模型",
                "kind": "str",
                "default": "deepseek-v4.1-flash",
                "choices": list(CHAT_MODELS),
            },
            {
                "key": "api_key_ref",
                "label": "密钥键名（可留空）",
                "kind": "str",
                "default": "WEFT_CHAT_API_KEY",
                "description": "只填 vault 里的键名，不要粘贴密钥本身；留空则用默认键名",
            },
            {
                "key": "system_prompt",
                "label": "系统提示词",
                "kind": "str",
                "default": "你是一个简洁友好的群聊助手，用中文回答，控制在 150 字以内。",
            },
            {"key": "max_tokens", "label": "最大回复 tokens", "kind": "int", "default": 800},
            {"key": "temperature", "label": "温度", "kind": "float", "default": 0.7},
            {"key": "timeout_seconds", "label": "超时(秒)", "kind": "float", "default": 90},
            {"key": "max_input_chars", "label": "输入上限（字）", "kind": "int", "default": 2000},
            {
                "key": "history_turns",
                "label": "上下文轮数",
                "kind": "int",
                "default": DEFAULT_HISTORY_TURNS,
                "description": "保留最近几轮对话（1 轮 = 一问一答）",
            },
            {"key": "retries", "label": "网关重试次数", "kind": "int", "default": 4},
            {
                "key": "retry_backoff_seconds",
                "label": "重试退避基数",
                "kind": "float",
                "default": 1.0,
            },
            {
                "key": "ack_text",
                "label": "回执文案",
                "kind": "str",
                "default": "🤔 思考中…",
                "description": "收到问题后先回的那句话。留空 = 不发回执（只发最终答案）。",
            },
        ]

    # -- 能力提供 --------------------------------------------------------- #
    secrets = {
        "api_key": SecretSlot(
            label="对话 API Key",
            ref="WEFT_CHAT_API_KEY",  # 绑已有键名，vault 零迁移
            description="OpenAI 兼容的 chat/completions 密钥",
        ),
    }

    def build_chat_service(self, pctx: ProviderContext) -> Any:
        """构造对话服务。凭据通过槽位 `api_key` 取，provider 不接触密钥。"""
        from .broker import LLMBroker
        from .config import ChatCfg
        from .providers.mock import MockChatProvider
        from .providers.openai_compat import OpenAICompatChatProvider

        cfg = ChatCfg.model_validate(dict(pctx.config))
        if cfg.provider == "mock":
            provider: Any = MockChatProvider()
        else:
            provider = OpenAICompatChatProvider(pctx.slot("api_key").headers, cfg)
        broker = LLMBroker(
            provider,
            model=cfg.model,
            system_prompt=cfg.system_prompt,
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
        )
        broker.max_input_chars = cfg.max_input_chars
        return broker

    # -- 入口 ------------------------------------------------------------- #
    @on_command("问", aliases=["ask", "ai"], usage="问 <问题>", priority=15, timeout=150)
    async def ask(self, ctx: Context, question: str = "") -> None:
        await self._answer(ctx, question, use_history=False)

    @on_message(require_at=True, priority=-30, block=True, timeout=150)
    async def talk(self, ctx: Context) -> None:
        text = ctx.event.text.strip() if ctx.event else ""
        if not text or text.startswith("/"):
            return
        await self._answer(ctx, text, use_history=True)

    # -- 主流程 ----------------------------------------------------------- #
    async def _answer(self, ctx: Context, question: str, *, use_history: bool) -> None:
        text = question.strip()
        if not text:
            await ctx.reply.text("用法：/问 <问题>，或者直接 @我 说点什么")
            return
        llm = ctx.service("llm:chat")
        ack_text = str(ctx.config.get("ack_text", "🤔 思考中…")).strip()
        if ack_text:
            await ctx.reply.ack(ack_text)
        messages = await self._build_messages(ctx, text, use_history=use_history)
        try:
            answer = await llm.chat(messages)
        except Exception as exc:  # noqa: BLE001
            ctx.log.exception("chat_failed", error_type=type(exc).__name__)
            await ctx.reply.text("对话失败，已记录 trace，请稍后再试")
            return
        if use_history:
            await self._remember(ctx, text, answer)
        await ctx.reply.text(answer)

    # -- 上下文 ----------------------------------------------------------- #
    @staticmethod
    def _history_key(ctx: Context) -> str:
        channel = ctx.event.channel if ctx.event else "unknown"
        actor = ctx.event.actor.id if ctx.event else "unknown"
        return f"hist:{channel}:{actor}"

    @staticmethod
    def _history_limit(ctx: Context) -> int:
        """保留多少条消息。`history_turns` 是轮数（1 轮 = 一问一答 = 2 条）。"""
        raw = ctx.config.get("history_turns", DEFAULT_HISTORY_TURNS)
        try:
            turns = max(0, int(raw))
        except (TypeError, ValueError):
            turns = DEFAULT_HISTORY_TURNS
        return turns * 2

    async def _build_messages(
        self, ctx: Context, text: str, *, use_history: bool
    ) -> list[dict[str, str]] | str:
        if not use_history or ctx.kv is None:
            return text
        try:
            raw = await ctx.kv.get(self._history_key(ctx), "[]")
            history = json.loads(raw) if raw else []
        except Exception:  # noqa: BLE001
            history = []
        if not isinstance(history, list):
            history = []
        cleaned = [
            {"role": m["role"], "content": m["content"]}
            for m in history
            if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
        ]
        limit = self._history_limit(ctx)
        return [*cleaned[-limit:], {"role": "user", "content": text}] if limit else text

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

    # -- 管理 ------------------------------------------------------------- #
    @on_command("重置对话", aliases=["reset"], usage="重置对话", priority=15, timeout=30)
    async def reset(self, ctx: Context) -> None:
        if ctx.kv is None:
            await ctx.reply.text("kv 能力未启用，无法重置上下文")
            return
        await ctx.kv.delete(self._history_key(ctx))
        await ctx.reply.text("上下文已清空")
