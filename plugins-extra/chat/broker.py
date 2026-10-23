"""LLM Broker —— 全框架唯一调用对话接口的地方。

和 `ImageBroker` 一样：插件只能拿到 `ctx.llm.chat(...)`，看不到 key 与 endpoint。
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from haiqbot.plugin_api import ProviderError, get_logger

log = get_logger(__name__)


@runtime_checkable
class ChatProvider(Protocol):
    name: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str: ...


class LLMBroker:
    def __init__(
        self,
        provider: ChatProvider,
        *,
        model: str,
        system_prompt: str = "",
        max_tokens: int = 800,
        temperature: float = 0.7,
    ) -> None:
        self.provider = provider
        self.model = model
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature

    async def chat(
        self,
        messages: str | list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        payload = self._with_system(messages)
        limit = int(getattr(self, "max_input_chars", 0) or 0)
        if limit:
            total = sum(len(str(m.get("content") or "")) for m in payload if m.get("role") != "system")
            if total > limit:
                raise ProviderError(f"输入太长（{total} 字 > 上限 {limit}），请精简后重试")
        t0 = time.time()
        text = await self.provider.complete(
            payload,
            model=model or self.model,
            max_tokens=max_tokens or self.max_tokens,
            temperature=self.temperature if temperature is None else temperature,
        )
        log.info(
            "llm_done",
            provider=getattr(self.provider, "name", "unknown"),
            model=model or self.model,
            msgs=len(payload),
            chars=len(text),
            elapsed_ms=int((time.time() - t0) * 1000),
        )
        return text

    def _with_system(self, messages: str | list[dict[str, str]]) -> list[dict[str, str]]:
        """既收单条问题（str），也收完整消息列表。

        单条问题时按 user 角色包一层，保证 system prompt 只有一个入口。
        """
        if isinstance(messages, str):
            turns = [{"role": "user", "content": messages}]
        else:
            turns = [dict(m) for m in messages]
        if not self.system_prompt:
            return turns
        if any(m.get("role") == "system" for m in turns):
            return turns
        return [{"role": "system", "content": self.system_prompt}, *turns]

    def describe(self) -> dict[str, Any]:
        return {
            "provider": getattr(self.provider, "name", "unknown"),
            "model": self.model,
            "has_system_prompt": bool(self.system_prompt),
        }
