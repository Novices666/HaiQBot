"""LLM 对话：provider / broker / 能力句柄 / chat 插件。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from haiqbot_local_assistant.chat.broker import LLMBroker
from haiqbot_local_assistant.chat.config import ChatCfg
from haiqbot_local_assistant.chat.providers.mock import MockChatProvider
from haiqbot_local_assistant.chat.providers.openai_compat import OpenAICompatChatProvider

from haiqbot.adapters.mock import MockAdapter
from haiqbot.app import App
from haiqbot.config import HaiqConfig, MediaCfg, PolicyCfg, QuotaCfg, WebuiCfg
from haiqbot.core.channel import Actor, ChannelId, ChannelKind
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.segment import Segment
from haiqbot.kernel.plugin import ServiceProxy
from haiqbot.kernel.vault import PlainFileBackend, Vault
from haiqbot.services.providers.base import ProviderError


# --------------------------------------------------------------------------- #
# provider
# --------------------------------------------------------------------------- #
def _auth(vault, ref: str = "HAIQ_CHAT_API_KEY"):
    """把 vault 适配成 provider 需要的鉴权头生成器。"""
    return lambda: {"Authorization": f"Bearer {vault.require(ref)}"}


def _vault(tmp_path: Path, value: str = "sk-chat-key-abcdefghijkl") -> Vault:
    vault = Vault([PlainFileBackend(tmp_path / "v.json")])
    vault.set("HAIQ_CHAT_API_KEY", value)
    return vault


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_provider_success(tmp_path: Path) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content),
                "path": request.url.path,
            }
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": "  你好呀  "}}]})

    cfg = ChatCfg(base_url="https://api.example.com/v1", api_key_ref="HAIQ_CHAT_API_KEY")
    provider = OpenAICompatChatProvider(_auth(_vault(tmp_path)), cfg, client=_client(handler))
    text = await provider.complete(
        [{"role": "user", "content": "hi"}],
        model="m1",
        max_tokens=100,
        temperature=0.5,
    )
    assert text == "你好呀"
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["auth"] == "Bearer sk-chat-key-abcdefghijkl"
    assert seen[0]["body"]["model"] == "m1"
    assert seen[0]["body"]["stream"] is False


async def test_provider_handles_content_array(tmp_path: Path) -> None:
    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": [{"text": "分段"}, {"text": "内容"}]}}]},
        )

    provider = OpenAICompatChatProvider(_auth(_vault(tmp_path)), ChatCfg(), client=_client(handler))
    assert await provider.complete([], model="m", max_tokens=10, temperature=0) == "分段内容"


async def test_provider_error_does_not_leak_key(tmp_path: Path) -> None:
    secret = "sk-chat-key-abcdefghijkl"

    def handler(_r: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"bad key {secret}"})

    provider = OpenAICompatChatProvider(_auth(_vault(tmp_path)), ChatCfg(), client=_client(handler))
    with pytest.raises(ProviderError) as exc:
        await provider.complete([], model="m", max_tokens=10, temperature=0)
    assert secret not in str(exc.value)
    assert "401" in str(exc.value)


async def test_provider_empty_choices(tmp_path: Path) -> None:
    provider = OpenAICompatChatProvider(
        _auth(_vault(tmp_path)),
        ChatCfg(),
        client=_client(lambda r: httpx.Response(200, json={"choices": []})),
    )
    with pytest.raises(ProviderError):
        await provider.complete([], model="m", max_tokens=10, temperature=0)


async def test_provider_missing_key(tmp_path: Path) -> None:
    vault = Vault([PlainFileBackend(tmp_path / "v.json")])
    provider = OpenAICompatChatProvider(
        _auth(vault, "NOT_SET"), ChatCfg(api_key_ref="NOT_SET"), client=_client(lambda r: httpx.Response(200))
    )
    with pytest.raises(Exception) as exc:
        await provider.complete([], model="m", max_tokens=10, temperature=0)
    assert "NOT_SET" in str(exc.value)


# --------------------------------------------------------------------------- #
# broker
# --------------------------------------------------------------------------- #
async def test_broker_prepends_system_prompt() -> None:
    provider = MockChatProvider(reply="R:")
    broker = LLMBroker(provider, model="m", system_prompt="SYSTEM", max_tokens=10, temperature=0.1)
    await broker.chat([{"role": "user", "content": "hi"}])
    assert provider.calls[0][0] == {"role": "system", "content": "SYSTEM"}
    assert provider.calls[0][1]["content"] == "hi"


async def test_broker_does_not_duplicate_system() -> None:
    provider = MockChatProvider()
    broker = LLMBroker(provider, model="m", system_prompt="SYSTEM")
    await broker.chat([{"role": "system", "content": "OWN"}, {"role": "user", "content": "x"}])
    roles = [m["role"] for m in provider.calls[0]]
    assert roles == ["system", "user"]
    assert provider.calls[0][0]["content"] == "OWN"


# --------------------------------------------------------------------------- #
# 服务代理（内核已无 LLMCap：输入校验/计费都归插件）
# --------------------------------------------------------------------------- #
async def test_service_proxy_forwards_chat() -> None:
    provider = MockChatProvider(reply="A:")
    proxy = ServiceProxy("llm:chat", lambda _n: LLMBroker(provider, model="m"))
    assert await proxy.chat([{"role": "user", "content": "你好"}]) == "A:你好"
    assert provider.calls[0] == [{"role": "user", "content": "你好"}]


async def test_service_proxy_carries_non_call_attrs() -> None:
    broker = LLMBroker(MockChatProvider(), model="m1", system_prompt="S")
    proxy = ServiceProxy("llm:chat", lambda _n: broker)
    assert proxy.model == "m1", "服务上的普通属性也要能透出来"


async def test_broker_prepends_system_prompt_once() -> None:
    provider = MockChatProvider()
    broker = LLMBroker(provider, model="m", system_prompt="SYS")
    await broker.chat([{"role": "user", "content": "q"}])
    assert [m["role"] for m in provider.calls[0]] == ["system", "user"]
    await broker.chat([{"role": "system", "content": "OWN"}, {"role": "user", "content": "q"}])
    assert [m["role"] for m in provider.calls[1]] == ["system", "user"]
    assert provider.calls[1][0]["content"] == "OWN"


# --------------------------------------------------------------------------- #
# chat 插件（走完整 App + mock provider）
# --------------------------------------------------------------------------- #
@pytest.fixture
def chat_cfg(tmp_path: Path) -> HaiqConfig:
    return HaiqConfig(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "haiq.db",
        secrets_dir=tmp_path / "secrets",
        log_level="CRITICAL",
        plugins_enabled=["echo", "assistant"],
        # command 模式 = 纯聊天：不出图、不做意图判断，行为完全可预期
        plugin_config={"assistant": {"router_mode": "command"}},
        quota=QuotaCfg(enabled=True, per_user_per_day=50, per_channel_per_day=50, rate_per_user_per_min=100),
        policy=PolicyCfg(handle_timeout_seconds=5.0),
        media=MediaCfg(dir=tmp_path / "data" / "media"),
        webui=WebuiCfg(enabled=False),
    )


def _event(text: str, msg_id: str, *, at: bool = False) -> MessageEvent:
    return MessageEvent(
        kind=EventKind.MESSAGE,
        channel=ChannelId("mock", ChannelKind.GROUP, "9"),
        actor=Actor(id="u-chat"),
        segments=[Segment.text(text)] + ([Segment.at("mock-bot")] if at else []),
        msg_id=msg_id,
        at_bot=at,
        bot_id="mock-bot",
    )


async def _boot(cfg: HaiqConfig) -> tuple[App, MockAdapter]:
    app = App(cfg)
    await app.setup(demo=True)
    await app.start_plugins()
    await app.start_adapters()
    mock = app.adapters["mock"]
    assert isinstance(mock, MockAdapter)
    # 默认全关语义：测试里要显式开启 mock 频道
    for _ch in ("mock:group:9", "mock:group:10001", "mock:group:1"):
        await app.store.channel_upsert(_ch, enabled=1, plugins=list(app.registry.handles()))
    app.runtime.channel_gate.invalidate()
    return app, mock


async def test_chat_command_replies(chat_cfg: HaiqConfig) -> None:
    app, mock = await _boot(chat_cfg)
    try:
        await mock.inject(_event("/问 你好", "c1"))
        texts = mock.texts()
        assert any("模拟回复" in t for t in texts), "应回模型答案"
    finally:
        await app.shutdown()


async def test_chat_command_without_question(chat_cfg: HaiqConfig) -> None:
    app, mock = await _boot(chat_cfg)
    try:
        await mock.inject(_event("/问", "c2"))
        assert any("用法" in t for t in mock.texts())
    finally:
        await app.shutdown()


async def test_at_message_uses_history(chat_cfg: HaiqConfig) -> None:
    app, mock = await _boot(chat_cfg)
    try:
        await mock.inject(_event("第一句", "h1", at=True))
        await asyncio.sleep(0.05)
        await mock.inject(_event("第二句", "h2", at=True))
        await asyncio.sleep(0.05)

        provider = app.runtime.get_service("llm:chat").provider
        assert len(provider.calls) == 2, "两次 @ 应各调一次模型"
        second = provider.calls[1]
        contents = [m["content"] for m in second if m["role"] != "system"]
        assert "第一句" in contents, "第二次应带上历史"
        assert contents[-1] == "第二句"
    finally:
        await app.shutdown()


async def test_plain_message_without_at_is_ignored(chat_cfg: HaiqConfig) -> None:
    app, mock = await _boot(chat_cfg)
    try:
        before = len(mock.sent)
        await mock.inject(_event("没有 @ 我", "noat"))
        assert len(mock.sent) == before, "未 @ 的情况下 chat 插件不该响应"
    finally:
        await app.shutdown()


async def test_chat_reset_clears_history(chat_cfg: HaiqConfig) -> None:
    app, mock = await _boot(chat_cfg)
    try:
        await mock.inject(_event("记住这个", "r1", at=True))
        await asyncio.sleep(0.05)
        await mock.inject(_event("/重置对话", "r2"))
        await mock.inject(_event("还记得吗", "r3", at=True))
        await asyncio.sleep(0.05)

        provider = app.runtime.get_service("llm:chat").provider
        last = provider.calls[-1]
        contents = [m["content"] for m in last if m["role"] != "system"]
        assert "记住这个" not in contents, "重置后不应再带旧历史"
    finally:
        await app.shutdown()


# --------------------------------------------------------------------------- #
# 总时限（budget）：不能让「单次超时 × 重试次数」把 handler 拖到被内核取消
#   真机事故（2026-10-28）：chat_timeout 90s × 5 次尝试 > handler 180s，
#   内核把 handler 取消，而 CancelledError 不是 Exception —— 连「对话失败」
#   都发不出去，群里表现就是「完全没反应」。
# --------------------------------------------------------------------------- #
async def test_broker_budget_becomes_provider_error() -> None:
    class _Slow:
        name = "slow"

        async def complete(self, messages, *, model, max_tokens, temperature, deadline=None):
            await asyncio.sleep(5)
            return "never"

    broker = LLMBroker(_Slow(), model="m")
    broker.budget_seconds = 0.05

    t0 = time.monotonic()
    with pytest.raises(ProviderError) as info:
        await broker.chat("hi")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, "预算到了就必须立刻失败，不能继续等"
    assert "超时" in str(info.value)


async def test_provider_splits_budget_across_attempts(tmp_path: Path) -> None:
    """剩余时间要摊到剩余尝试上 —— 否则第一次就把 90s 用光。"""
    timeouts: list[float | None] = []

    class _Client:
        async def post(self, url, *, json=None, headers=None, timeout=None):
            timeouts.append(timeout)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = ChatCfg(base_url="https://api.example.com/v1", timeout_seconds=90, retries=2)
    provider = OpenAICompatChatProvider(_auth(_vault(tmp_path)), cfg, client=_Client())

    text = await provider.complete(
        [{"role": "user", "content": "hi"}],
        model="m1",
        max_tokens=10,
        temperature=0.1,
        deadline=time.monotonic() + 9.0,
    )

    assert text == "ok"
    assert len(timeouts) == 1
    assert timeouts[0] is not None
    # attempts = retries + 1 = 3 → 9s / 3 = 3s
    assert 2.5 <= float(timeouts[0]) <= 3.0


async def test_provider_fails_fast_when_budget_already_gone(tmp_path: Path) -> None:
    sent: list[float | None] = []

    class _Client:
        async def post(self, url, *, json=None, headers=None, timeout=None):
            sent.append(timeout)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    cfg = ChatCfg(base_url="https://api.example.com/v1", timeout_seconds=90, retries=4)
    provider = OpenAICompatChatProvider(_auth(_vault(tmp_path)), cfg, client=_Client())

    with pytest.raises(ProviderError) as info:
        await provider.complete(
            [{"role": "user", "content": "hi"}],
            model="m1",
            max_tokens=10,
            temperature=0.1,
            deadline=time.monotonic() - 1,
        )

    assert "超时" in str(info.value)
    assert sent == [], "预算已用尽时不该再发请求"


async def test_chat_service_takes_budget_from_config() -> None:
    """`chat_budget_seconds` 必须真的传到 broker 上（否则配了也白配）。"""
    from haiqbot.kernel.plugin import ProviderContext
    from haiqbot.kernel.plugin_sources import load_local_module

    # 插件是按 `haiqbot_local_<id>` 动态注册的：别的测试会把它从 sys.modules 里
    # 挤掉再重载，所以这里显式按目录加载一次，别依赖进程里残留的模块对象。
    plugin_dir = Path(__file__).resolve().parent.parent / "plugins-extra" / "assistant"
    mod = load_local_module("assistant", plugin_dir)

    pctx = ProviderContext(
        plugin_id="assistant",
        config={"chat_provider": "mock", "chat_budget_seconds": 42},
    )
    service = mod.chat.build_service(pctx)
    assert service.budget_seconds == 42


async def test_default_budget_stays_below_handler_timeout() -> None:
    """默认总时限必须小于 180s 的 handler 超时，否则失败时群里一句话都没有。"""
    cfg = ChatCfg()
    assert 0 < cfg.budget_seconds < 180
