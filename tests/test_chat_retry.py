"""网关抖动重试：429/5xx 指数退避，4xx 立即失败。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from haiqbot_local_assistant.chat.config import ChatCfg
from haiqbot_local_assistant.chat.providers.openai_compat import RETRYABLE_STATUS, OpenAICompatChatProvider

from haiqbot.kernel.vault import PlainFileBackend, Vault
from haiqbot.services.providers.base import ProviderError


def _auth(vault, ref: str = "HAIQ_CHAT_API_KEY"):
    """把 vault 适配成 provider 需要的鉴权头生成器。"""

    return lambda: {"Authorization": f"Bearer {vault.require(ref)}"}


def _provider(tmp_path: Path, handler, **cfg_kwargs) -> OpenAICompatChatProvider:

    vault = Vault([PlainFileBackend(tmp_path / "v.json")])

    vault.set("HAIQ_CHAT_API_KEY", "sk-retry-test-1234567890")

    base: dict = {
        "api_key_ref": "HAIQ_CHAT_API_KEY",
        "retries": 2,
        "retry_backoff_seconds": 0.0,  # 测试不睡觉
    }

    base.update(cfg_kwargs)

    cfg = ChatCfg(**base)

    return OpenAICompatChatProvider(
        _auth(vault), cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def test_retryable_set() -> None:

    assert {429, 500, 502, 503, 504} <= RETRYABLE_STATUS

    assert 400 not in RETRYABLE_STATUS

    assert 401 not in RETRYABLE_STATUS


async def test_503_then_success(tmp_path: Path) -> None:

    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:

        calls.append(1)

        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"message": "Service temporarily unavailable"}})

        return httpx.Response(200, json={"choices": [{"message": {"content": "恢复了"}}]})

    provider = _provider(tmp_path, handler)

    text = await provider.complete(
        [{"role": "user", "content": "hi"}], model="m", max_tokens=10, temperature=0
    )

    assert text == "恢复了"

    assert len(calls) == 2, "503 后应重试一次"


async def test_retries_exhausted_raises_with_status(tmp_path: Path) -> None:

    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:

        calls.append(1)

        return httpx.Response(503, json={"error": {"message": "down"}})

    provider = _provider(tmp_path, handler, retries=1)

    with pytest.raises(ProviderError) as exc:
        await provider.complete([{"role": "user", "content": "hi"}], model="m", max_tokens=10, temperature=0)

    assert len(calls) == 2, "retries=1 → 共 2 次尝试"

    assert "503" in str(exc.value)


async def test_non_retryable_fails_immediately(tmp_path: Path) -> None:

    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:

        calls.append(1)

        return httpx.Response(401, json={"error": {"message": "bad key"}})

    provider = _provider(tmp_path, handler)

    with pytest.raises(ProviderError) as exc:
        await provider.complete([{"role": "user", "content": "hi"}], model="m", max_tokens=10, temperature=0)

    assert len(calls) == 1, "401 不该重试"

    assert "401" in str(exc.value)

    # 错误体里的内容不得被带出

    assert "bad key" not in str(exc.value)


async def test_network_error_retried_then_raises(tmp_path: Path) -> None:

    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:

        calls.append(1)

        raise httpx.ConnectError("boom")

    provider = _provider(tmp_path, handler, retries=2)

    with pytest.raises(ProviderError) as exc:
        await provider.complete([{"role": "user", "content": "hi"}], model="m", max_tokens=10, temperature=0)

    assert len(calls) == 3

    assert "ConnectError" in str(exc.value)


async def test_zero_retries_means_single_attempt(tmp_path: Path) -> None:

    calls: list[int] = []

    def handler(_r: httpx.Request) -> httpx.Response:

        calls.append(1)

        return httpx.Response(503)

    provider = _provider(tmp_path, handler, retries=0)

    with pytest.raises(ProviderError):
        await provider.complete([{"role": "user", "content": "hi"}], model="m", max_tokens=10, temperature=0)

    assert len(calls) == 1


async def test_payload_shape(tmp_path: Path) -> None:

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:

        seen.append(json.loads(request.content))

        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = _provider(tmp_path, handler)

    await provider.complete([{"role": "user", "content": "x"}], model="mm", max_tokens=42, temperature=0.3)

    body = seen[0]

    assert body == {
        "model": "mm",
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 42,
        "temperature": 0.3,
        "stream": False,
    }
