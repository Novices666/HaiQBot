"""OpenAI 兼容的 `/chat/completions` provider。

密钥只从 vault 取，取完立刻进脱敏名单；任何异常信息都不含密钥。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from haiqbot.plugin_api import ProviderError, get_logger

from ..config import ChatCfg

log = get_logger(__name__)

# 网关的瞬时故障，值得重试
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# 单次退避上限；503 常成簇出现（实测 3 连），但重试本身只要 0.3s，等待不必太长
MAX_BACKOFF_SECONDS = 4.0


def backoff_delay(base: float, attempt: int) -> float:
    """第 attempt 次失败后应等多久（attempt 从 0 开始）。"""
    if base <= 0:
        return 0.0
    return min(base * (2**attempt), MAX_BACKOFF_SECONDS)


class OpenAICompatChatProvider:
    name = "openai_compat"

    def __init__(
        self,
        auth: Callable[[], dict[str, str]],
        cfg: ChatCfg,
        *,
        client: Any = None,
        timeout: float | None = None,
    ) -> None:
        self._auth = auth
        self.cfg = cfg
        self._client = client
        self.timeout = timeout if timeout is not None else cfg.timeout_seconds

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
            self._client = None

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        headers = self._auth()
        attempts = max(1, int(self.cfg.retries) + 1)
        last_status: int | None = None
        for attempt in range(attempts):
            is_last = attempt == attempts - 1
            try:
                resp = await self._http().post(url, json=payload, headers=headers)
            except Exception as exc:  # noqa: BLE001 - 网络异常可能带 URL
                if is_last:
                    raise ProviderError(f"对话请求失败：{type(exc).__name__}") from exc
                log.warning("llm_retry", reason=type(exc).__name__, attempt=attempt + 1)
                await asyncio.sleep(backoff_delay(self.cfg.retry_backoff_seconds, attempt))
                continue
            if resp.status_code in RETRYABLE_STATUS and not is_last:
                last_status = resp.status_code
                log.warning("llm_retry", status=resp.status_code, attempt=attempt + 1)
                await asyncio.sleep(backoff_delay(self.cfg.retry_backoff_seconds, attempt))
                continue
            if resp.status_code >= 400:
                # 只暴露状态码，绝不回显请求体 / header
                raise ProviderError(f"对话接口返回 {resp.status_code}")
            try:
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise ProviderError("对话接口返回了非 JSON 响应") from exc
            return self._extract(body)
        raise ProviderError(f"对话接口多次重试仍失败（最后一次 {last_status}）")

    @staticmethod
    def _extract(body: dict[str, Any]) -> str:
        choices = body.get("choices") or []
        if not choices:
            raise ProviderError("对话接口未返回 choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # 少数网关返回分段数组
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not content:
            raise ProviderError("对话接口返回了空内容")
        return str(content).strip()
