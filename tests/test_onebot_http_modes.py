"""OneBot HTTP / SSE 连接模式：正向 HTTP 调用、反向 HTTP 上报、上报鉴权。

WS 两种模式已有测试（test_adapter_onebot11 / test_onebot_recv_loop），
这里只管新增的两条路：`mode="http"` 与 `mode="sse"`。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from typing import Any

import httpx
import pytest

from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import OneBotCfg
from haiqbot.core.errors import AdapterError

GROUP_PAYLOAD = {
    "post_type": "message",
    "message_type": "group",
    "group_id": 200000001,
    "user_id": 100000001,
    "message_id": 7,
    "message": "你好",
    "sender": {"nickname": "小明"},
}


def _adapter(handler: Any = None, *, token: str | None = "tok", **cfg: Any) -> OneBotAdapter:
    adapter = OneBotAdapter(
        OneBotCfg(mode="http", http_api_url="http://api.test", **cfg),
        token_provider=lambda: token,
        secret_provider=lambda ref: {"WEBHOOK_SECRET": "s3cret"}.get(ref),
    )
    if handler is not None:
        adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return adapter


# --------------------------------------------------------------------------- #
# 正向 HTTP 调用
# --------------------------------------------------------------------------- #
async def test_http_call_posts_action_with_bearer() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok", "retcode": 0, "data": {"user_id": 42}})

    adapter = _adapter(handler)
    payload = await adapter.call("get_login_info", {})
    # call() 返回完整回包（和 WS 模式一致），取数据由调用方看 "data"
    assert payload["data"] == {"user_id": 42}
    assert payload["retcode"] == 0

    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == "http://api.test/get_login_info"
    assert req.headers["Authorization"] == "Bearer tok"
    assert json.loads(req.content) == {}
    assert adapter.health()["connected"] is True
    await adapter.stop()


async def test_http_call_surfaces_retcode_as_adapter_error() -> None:
    adapter = _adapter(
        lambda request: httpx.Response(
            200, json={"status": "failed", "retcode": 1404, "message": "不支持的API"}
        )
    )
    with pytest.raises(AdapterError, match="1404"):
        await adapter.call("get_user_status", {"user_id": "1"})
    await adapter.stop()


async def test_http_call_surfaces_http_status_and_clears_connected() -> None:
    adapter = _adapter(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(AdapterError, match="500"):
        await adapter.call("send_private_msg", {})
    health = adapter.health()
    assert health["connected"] is False
    assert "500" in (health["last_error"] or "")
    await adapter.stop()


async def test_http_call_rejects_non_json() -> None:
    adapter = _adapter(lambda request: httpx.Response(200, text="<html>"))
    with pytest.raises(AdapterError, match="不是 JSON"):
        await adapter.call("get_status", {})
    await adapter.stop()


async def test_call_routes_to_http_mode_not_ws() -> None:
    """http 模式下没有 WS 连接也不该报「OneBot 未连接」。"""
    adapter = _adapter(lambda request: httpx.Response(200, json={"status": "ok", "retcode": 0, "data": []}))
    payload = await adapter.call("get_group_list", {})
    assert payload["data"] == []
    await adapter.stop()


# --------------------------------------------------------------------------- #
# 上报鉴权
# --------------------------------------------------------------------------- #
def _headers(**kw: str) -> Any:
    class _H(dict):
        def get(self, key: str, default: Any = None) -> Any:  # noqa: D401
            return super().get(key.lower(), default)

    return _H({k.lower(): v for k, v in kw.items()})


def test_webhook_verify_bearer_token() -> None:
    adapter = _adapter()
    assert adapter._verify_webhook(_headers(authorization="Bearer tok"), b"{}") is True
    assert adapter._verify_webhook(_headers(authorization="Bearer wrong"), b"{}") is False
    assert adapter._verify_webhook(_headers(), b"{}") is False


def test_webhook_verify_hmac_signature_with_explicit_secret() -> None:
    adapter = _adapter(webhook_secret_ref="WEBHOOK_SECRET")
    body = b'{"post_type":"meta_event"}'
    good = "sha1=" + hmac.new(b"s3cret", body, hashlib.sha1).hexdigest()
    assert adapter._verify_webhook(_headers(**{"X-Signature": good}), body) is True
    assert adapter._verify_webhook(_headers(**{"X-Signature": "sha1=deadbeef"}), body) is False
    # 带了签名但签名错：即使 Bearer 正确也不再放行（签名优先）
    assert (
        adapter._verify_webhook(
            _headers(**{"X-Signature": "sha1=deadbeef"}, authorization="Bearer tok"), body
        )
        is False
    )


def test_webhook_signature_falls_back_to_access_token() -> None:
    """真机 NapCat 实测：X-Signature 用的是 access_token 当 HMAC 密钥。

    没配 webhook_secret_ref 时，带签名的上报必须能通过 —— 否则全是 401。
    """
    adapter = _adapter()  # token="tok"，webhook_secret_ref 为空
    body = b'{"post_type":"meta_event","self_id":1}'
    good = "sha1=" + hmac.new(b"tok", body, hashlib.sha1).hexdigest()
    assert adapter._verify_webhook(_headers(**{"X-Signature": good}), body) is True
    assert adapter._verify_webhook(_headers(**{"X-Signature": "sha1=bad"}), body) is False


def test_webhook_open_when_no_auth_configured() -> None:
    adapter = _adapter(token=None)
    assert adapter._verify_webhook(_headers(), b"{}") is True


# --------------------------------------------------------------------------- #
# 收事件
# --------------------------------------------------------------------------- #
async def test_ingest_resolves_pending_echo() -> None:
    adapter = _adapter(token=None)
    fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    adapter._pending["haiqbot-1"] = fut
    await adapter._ingest({"echo": "haiqbot-1", "status": "ok", "data": {}})
    assert fut.done()
    assert adapter._pending == {}


async def test_ingest_dispatches_event(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter(token=None)
    seen: list[dict[str, Any]] = []

    async def fake_handle(payload: dict[str, Any]) -> None:
        seen.append(payload)

    monkeypatch.setattr(adapter, "_handle_event", fake_handle)
    await adapter._ingest(GROUP_PAYLOAD)
    await asyncio.sleep(0)
    assert seen == [GROUP_PAYLOAD]
    await adapter.stop()


def _wait_flag(flag: threading.Event, seconds: float = 3.0) -> bool:
    return flag.wait(seconds)


def test_webhook_endpoint_accepts_event_and_rejects_bad_token() -> None:
    from starlette.testclient import TestClient

    monkeypatch_token = "tok"
    adapter = OneBotAdapter(
        OneBotCfg(mode="http"),
        token_provider=lambda: monkeypatch_token,
    )
    received: list[Any] = []
    flag = threading.Event()

    def push(event: Any) -> None:
        received.append(event)
        flag.set()

    adapter.bind(push)
    client = TestClient(adapter._build_webhook_app())

    bad = client.post("/onebot", json=GROUP_PAYLOAD)
    assert bad.status_code == 401

    ok = client.post("/onebot", json=GROUP_PAYLOAD, headers={"Authorization": "Bearer tok"})
    assert ok.status_code == 204
    assert _wait_flag(flag), "合法上报必须被派发出去"
    assert received and received[0].text == "你好"

    broken = client.post("/onebot", content=b"not-json", headers={"Authorization": "Bearer tok"})
    assert broken.status_code == 400


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
async def test_sse_stream_feeds_events() -> None:
    payload = GROUP_PAYLOAD
    body = ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) > 1:
            raise httpx.ConnectError("stop retrying")
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    adapter = OneBotAdapter(
        OneBotCfg(mode="sse", http_api_url="http://api.test", sse_path="/_events"),
        token_provider=lambda: "tok",
    )
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    received: list[Any] = []
    flag = threading.Event()

    def push(event: Any) -> None:
        received.append(event)
        flag.set()

    adapter.bind(push)
    task = asyncio.create_task(adapter._run_sse())
    for _ in range(40):
        if flag.is_set():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.stop()

    assert received and received[0].text == "你好"
    assert str(calls[0].url) == "http://api.test/_events"
    assert calls[0].headers["Authorization"] == "Bearer tok"


def test_mode_choices_exposed() -> None:
    for mode in ("client", "server", "http", "sse"):
        assert OneBotCfg(mode=mode).mode == mode


async def test_sse_backs_off_after_clean_stream_close() -> None:
    """流正常结束（协议端重启）也必须退避，不能变成热循环重连。"""
    calls: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, content=b'data: {"post_type":"meta_event","meta_event_type":"heartbeat"}\n\n'
        )

    adapter = OneBotAdapter(
        OneBotCfg(mode="sse", http_api_url="http://api.test", sse_path="/_events"),
        token_provider=lambda: None,
    )
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter.bind(lambda event: None)

    task = asyncio.create_task(adapter._run_sse())
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.stop()
    assert len(calls) == 1, f"流结束后立刻重连了（{len(calls)} 次）"


async def test_sse_url_overrides_derived_endpoint() -> None:
    """NapCat 的 API 在 3000、SSE 在 3003，必须能单独指定 SSE 地址。"""
    seen: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"")

    adapter = OneBotAdapter(
        OneBotCfg(
            mode="sse",
            http_api_url="http://api.test:3000",
            sse_url="http://sse.test:3003/_events",
            sse_path="/_events",
        ),
        token_provider=lambda: None,
    )
    adapter._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter.bind(lambda event: None)
    task = asyncio.create_task(adapter._run_sse())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await adapter.stop()
    assert str(seen[0].url) == "http://sse.test:3003/_events"
