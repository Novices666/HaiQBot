"""回归测试：收包循环不能被事件处理阻塞（否则 send_* 死锁到 api_timeout）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import OneBotCfg


class FakeWS:
    """假的 OneBot WS：send 会立刻把回包塞回收包队列。"""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return json.dumps(item)

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        self.inbox.put_nowait({"status": "ok", "retcode": 0, "echo": msg["echo"], "data": {"message_id": 42}})

    async def close(self) -> None:
        self.inbox.put_nowait(None)


def _group_message() -> dict[str, Any]:
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1001,
        "user_id": 2002,
        "message_id": 3003,
        "message": [{"type": "text", "data": {"text": "/画图 猫"}}],
    }


async def test_event_handler_can_call_api_without_deadlock() -> None:
    """事件处理里调 send_group_msg 必须能拿到回包。

    修复前：_recv_loop inline await 事件处理 → 回包永远读不到 → api_timeout。
    """
    ws = FakeWS()
    adapter = OneBotAdapter(
        OneBotCfg(enabled=True, api_timeout_seconds=0.5),
        lambda: "tok",
    )

    results: list[dict[str, Any]] = []
    done = asyncio.Event()

    async def push(event: Any) -> None:
        results.append(await adapter.call("send_group_msg", {"group_id": 1001, "message": []}))
        done.set()

    adapter.bind(push)
    adapter._ws = ws  # call() 通过 self._ws 发送
    ws.inbox.put_nowait(_group_message())

    task = asyncio.create_task(adapter._recv_loop(ws))
    try:
        await asyncio.wait_for(done.wait(), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert results, "事件处理中的 API 调用没有完成"
    assert results[0]["status"] == "ok"
    assert ws.sent[0]["action"] == "send_group_msg"


async def test_recv_loop_keeps_reading_while_handler_sleeps() -> None:
    """处理事件期间，收包循环仍应继续处理后续事件。"""
    ws = FakeWS()
    adapter = OneBotAdapter(OneBotCfg(enabled=True), lambda: "tok")

    seen: list[str] = []
    gate = asyncio.Event()

    async def push(event: Any) -> None:
        seen.append(event.text)
        await gate.wait()  # 卡住第一个事件

    adapter.bind(push)
    ws.inbox.put_nowait(_group_message())
    ws.inbox.put_nowait(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1001,
            "user_id": 2002,
            "message_id": 3004,
            "message": [{"type": "text", "data": {"text": "第二条"}}],
        }
    )

    task = asyncio.create_task(adapter._recv_loop(ws))
    try:
        await asyncio.sleep(0.2)
        # 第一个事件被 gate 卡住，第二个仍应进来
        assert len(seen) == 2, f"收包循环被阻塞，只收到 {len(seen)} 条"
        gate.set()
        await asyncio.sleep(0.05)
    finally:
        gate.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_handler_exception_does_not_kill_recv_loop() -> None:
    ws = FakeWS()
    adapter = OneBotAdapter(OneBotCfg(enabled=True), lambda: "tok")
    seen: list[str] = []

    async def push(event: Any) -> None:
        seen.append(event.text)
        if len(seen) == 1:
            raise RuntimeError("boom")

    adapter.bind(push)
    ws.inbox.put_nowait(_group_message())
    ws.inbox.put_nowait(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 1001,
            "user_id": 2002,
            "message_id": 3005,
            "message": [{"type": "text", "data": {"text": "after-error"}}],
        }
    )

    task = asyncio.create_task(adapter._recv_loop(ws))
    try:
        await asyncio.sleep(0.2)
        assert seen == ["/画图 猫", "after-error"], "单事件异常不应中断收包"
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_stop_cancels_pending_event_tasks() -> None:
    ws = FakeWS()
    adapter = OneBotAdapter(OneBotCfg(enabled=True), lambda: "tok")
    started = asyncio.Event()
    gate = asyncio.Event()

    async def push(event: Any) -> None:
        started.set()
        await gate.wait()

    adapter.bind(push)
    ws.inbox.put_nowait(_group_message())

    task = asyncio.create_task(adapter._recv_loop(ws))
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert adapter._event_tasks

    await adapter.stop()
    gate.set()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert adapter._event_tasks == set()
