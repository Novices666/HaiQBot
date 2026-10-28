"""LLM Broker —— 全框架唯一调用对话接口的地方。

和 `ImageBroker` 一样：插件只能拿到 `ctx.llm.chat(...)`，看不到 key 与 endpoint。
"""

from __future__ import annotations

import asyncio
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
        deadline: float | None = None,
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
        #: 一次对话的**总时限**（秒，含重试）。0 = 不限。
        #: 为什么要它：单次请求超时 × 重试次数 很容易超过外层 handler 的超时，
        #: 那样内核会在半路把 handler 取消 —— 连「失败了」这句话都发不出去。
        self.budget_seconds = 0.0
        #: 追加到 system 的固定说明（由插件组装服务时填）。与 system_prompt 分开：
        #: 「用户可配的人设」和「协议层的必要说明」互不污染。
        self.system_extra = ""

    async def chat(
        self,
        messages: str | list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        budget: float | None = None,
    ) -> str:
        payload = self._with_system(messages)
        limit = int(getattr(self, "max_input_chars", 0) or 0)
        if limit:
            total = sum(len(str(m.get("content") or "")) for m in payload if m.get("role") != "system")
            if total > limit:
                raise ProviderError(f"输入太长（{total} 字 > 上限 {limit}），请精简后重试")

        budget_s = self.budget_seconds if budget is None else budget
        deadline = time.monotonic() + float(budget_s) if budget_s and budget_s > 0 else None

        t0 = time.time()
        try:
            if deadline is None:
                text = await self.provider.complete(
                    payload,
                    model=model or self.model,
                    max_tokens=max_tokens or self.max_tokens,
                    temperature=self.temperature if temperature is None else temperature,
                )
            else:
                # `asyncio.timeout` 是兜底：provider 内部会按剩余预算切分单次超时，
                # 但任何一层卡住（DNS、坏实现）都不能让整次对话无限拖下去。
                async with asyncio.timeout(budget_s):
                    text = await self.provider.complete(
                        payload,
                        model=model or self.model,
                        max_tokens=max_tokens or self.max_tokens,
                        temperature=self.temperature if temperature is None else temperature,
                        deadline=deadline,
                    )
        except TimeoutError as exc:
            # 转成 ProviderError：调用方只需要认识一种失败类型，
            # 而「超时」这句话必须能被发到群里（所以不能是 CancelledError）。
            log.warning("llm_budget_exhausted", budget_s=budget_s)
            raise ProviderError(f"对话超时（{float(budget_s or 0):.0f} 秒内没返回）") from exc

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
        system = "\n\n".join(part for part in (self.system_prompt, self.system_extra) if part)
        if not system:
            return turns
        if any(m.get("role") == "system" for m in turns):
            return turns
        return [{"role": "system", "content": system}, *turns]

    def describe(self) -> dict[str, Any]:
        return {
            "provider": getattr(self.provider, "name", "unknown"),
            "model": self.model,
            "has_system_prompt": bool(self.system_prompt),
        }
