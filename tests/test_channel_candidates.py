"""频道候选清单：面板「新建 / 开启一个频道」那个可搜索下拉框的数据源。

要点（都是"面板会坑人"的地方）：
1. 协议端拿不到列表时要**说清楚**，而且不能让整个接口失败 ——
   已配置的和最近出现过的仍然要列出来，否则用户没法新建；
2. 已配置的频道要标出来（`configured` / `enabled`），并按"最常改的排前面"排序；
3. 库里手工写坏的字符串不能让整份列表崩。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from haiqbot.kernel.store import Store
from haiqbot.webui.api import channels as channels_api
from haiqbot.webui.api.common import Ctx


class _Store:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, seen: list[dict[str, Any]] | None = None
    ) -> None:
        self._rows = rows or []
        self._seen = seen or []

    async def channel_all(self) -> list[dict[str, Any]]:
        return self._rows

    async def seen_channels(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self._seen[:limit]


class _Adapter:
    def __init__(
        self,
        groups: list[dict[str, Any]] | None = None,
        friends: list[dict[str, Any]] | None = None,
        error: str = "",
    ) -> None:
        self._groups = groups or []
        self._friends = friends or []
        self._error = error

    async def group_list(self) -> list[dict[str, Any]]:
        if self._error:
            raise RuntimeError(self._error)
        return self._groups

    async def friend_list(self) -> list[dict[str, Any]]:
        if self._error:
            raise RuntimeError(self._error)
        return self._friends


def _client(
    *,
    adapters: dict[str, Any] | None = None,
    rows: list[dict[str, Any]] | None = None,
    seen: list[dict[str, Any]] | None = None,
) -> TestClient:
    runtime = SimpleNamespace(store=_Store(rows, seen), adapters=adapters or {})
    ctx = Ctx(runtime=runtime, registry=None, auth=None, config_path=Path("haiq.toml"))
    return TestClient(Starlette(routes=channels_api.routes(ctx)))


def _items(client: TestClient) -> tuple[list[dict[str, Any]], list[str]]:
    payload = client.get("/api/channels/candidates").json()
    assert payload["ok"] is True
    return payload["data"]["items"], payload["data"]["errors"]


def test_real_groups_and_friends_become_candidates() -> None:
    client = _client(
        adapters={
            "onebot": _Adapter(
                groups=[{"group_id": 674425800, "group_name": "初尘带飞"}],
                friends=[{"user_id": 10001, "remark": "小号", "nickname": "阿尘"}],
            )
        }
    )
    items, errors = _items(client)
    assert errors == []
    channels = {item["channel"]: item for item in items}
    group = channels["onebot:group:674425800"]
    assert group["label"].startswith("初尘带飞（674425800）")
    assert group["sources"] == ["协议端"]
    assert group["configured"] is False
    private = channels["onebot:private:10001"]
    assert private["label"].startswith("小号（10001）")
    assert private["kind"] == "private"


def test_configured_channels_are_flagged_and_sorted_first() -> None:
    client = _client(
        adapters={"onebot": _Adapter(groups=[{"group_id": 1, "group_name": "某群"}])},
        rows=[{"channel": "onebot:group:674425800", "enabled": 1, "plugins": ["echo"]}],
    )
    items, _ = _items(client)
    assert items[0]["channel"] == "onebot:group:674425800"
    assert items[0]["configured"] is True
    assert items[0]["enabled"] is True
    assert items[0]["sources"] == ["已配置"]


def test_disabled_channel_is_flagged_as_not_enabled() -> None:
    client = _client(rows=[{"channel": "onebot:group:2", "enabled": 0, "plugins": []}])
    items, _ = _items(client)
    assert items[0]["configured"] is True
    assert items[0]["enabled"] is False


def test_seen_channel_is_merged_into_existing_candidate() -> None:
    """同一个群来自三个来源时只出现一行，但来源都记着。"""
    client = _client(
        adapters={"onebot": _Adapter(groups=[{"group_id": 5, "group_name": "群五"}])},
        rows=[{"channel": "onebot:group:5", "enabled": 1}],
        seen=[{"channel": "onebot:group:5", "last_ts": 1.0}],
    )
    items, _ = _items(client)
    assert len(items) == 1
    assert sorted(items[0]["sources"]) == ["协议端", "已配置", "最近出现"]


def test_adapter_failure_is_reported_but_other_sources_survive() -> None:
    client = _client(
        adapters={"onebot": _Adapter(error="websocket 已断开")},
        rows=[{"channel": "onebot:group:674425800", "enabled": 1}],
        seen=[{"channel": "onebot:private:10001", "last_ts": 2.0}],
    )
    items, errors = _items(client)
    channels = {item["channel"] for item in items}
    # 协议端挂了，但"已配置"和"最近出现"仍然在 —— 面板不会变成空下拉
    assert "onebot:group:674425800" in channels
    assert "onebot:private:10001" in channels
    assert len(errors) == 2
    assert all("websocket 已断开" in message for message in errors)


def test_broken_channel_string_is_skipped() -> None:
    client = _client(rows=[{"channel": "不是频道", "enabled": 1}])
    items, _ = _items(client)
    assert items == []


def test_adapter_without_listing_methods_is_ignored() -> None:
    client = _client(adapters={"official": SimpleNamespace()})
    items, errors = _items(client)
    assert items == []
    assert errors == []


def test_unknown_kind_channel_is_skipped() -> None:
    """真机上出现过 `onebot:unknown:`（审计里没类型的事件）：没有 id，选了也配不出东西。"""
    client = _client(seen=[{"channel": "onebot:unknown:", "last_ts": 3.0}])
    items, _ = _items(client)
    assert items == []


@pytest.mark.asyncio
async def test_store_seen_channels_merges_audit_and_invocation(tmp_path: Path) -> None:
    store = await Store(tmp_path / "haiq.db").open()
    try:
        await store.audit(
            trace_id=None,
            plugin_id="echo",
            action="invoked",
            channel="onebot:group:1",
            actor="u1",
            detail="{}",
        )
        await store.invocation_add(
            trace_id=None,
            plugin_id="assistant",
            capability="llm:chat",
            provider="openai_compat",
            model="m",
            request="{}",
            detail="{}",
            duration_ms=10,
            status="ok",
            error=None,
            channel="onebot:group:2",
            actor="u2",
            media_id=None,
        )
        rows = await store.seen_channels(limit=10)
    finally:
        await store.close()
    channels = [row["channel"] for row in rows]
    assert channels[0] == "onebot:group:2"  # 后写的排前面
    assert set(channels) == {"onebot:group:1", "onebot:group:2"}
