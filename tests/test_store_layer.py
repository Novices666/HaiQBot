"""数据层：调用流水、群维度配置、media.plugin_id 与老库迁移。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from haiqbot.kernel.store import Store


@pytest.fixture
async def store(tmp_path: Path):
    s = await Store(tmp_path / "t.db").open()
    try:
        yield s
    finally:
        await s.close()


# -- invocation ------------------------------------------------------------- #


async def test_invocation_roundtrip(store: Store) -> None:
    rid = await store.invocation_add(
        capability="image:generate",
        plugin_id="painter",
        trace_id="t1",
        provider="openai_compat",
        model="gpt-image-2",
        request="一只猫",
        detail='{"size": "1024x1024"}',
        duration_ms=35000,
        channel="onebot:group:1",
        actor="u1",
        media_id="m1",
    )
    assert rid > 0

    rows = await store.invocation_recent(10)
    assert len(rows) == 1
    row = rows[0]
    assert row["capability"] == "image:generate"
    assert row["request"] == "一只猫"
    assert row["duration_ms"] == 35000
    assert row["status"] == "ok"


async def test_invocation_records_failures(store: Store) -> None:
    await store.invocation_add(
        capability="llm:chat", plugin_id="chat", status="error", error="503 Service Unavailable"
    )
    rows = await store.invocation_recent(10)
    assert rows[0]["status"] == "error"
    assert "503" in rows[0]["error"]


async def test_invocation_filters(store: Store) -> None:
    await store.invocation_add(capability="image:generate", plugin_id="painter")
    await store.invocation_add(capability="llm:chat", plugin_id="chat")
    await store.invocation_add(capability="llm:chat", plugin_id="chat")

    assert len(await store.invocation_recent(10, plugin_id="chat")) == 2
    assert len(await store.invocation_recent(10, capability="image:generate")) == 1
    assert len(await store.invocation_recent(10)) == 3


async def test_invocation_delete_by_ids_and_by_window(store: Store) -> None:
    a = await store.invocation_add(capability="llm:chat", plugin_id="chat")
    await store.invocation_add(capability="llm:chat", plugin_id="chat")

    assert await store.invocation_delete(ids=[a]) == 1
    assert len(await store.invocation_recent(10)) == 1

    # 时间窗删：把所有早于「现在+1秒」的都清掉
    assert await store.invocation_delete(before=time.time() + 1) == 1
    assert await store.invocation_recent(10) == []


async def test_invocation_delete_refuses_unbounded(store: Store) -> None:
    with pytest.raises(ValueError):
        await store.invocation_delete()


async def test_invocation_stats(store: Store) -> None:
    await store.invocation_add(capability="llm:chat", duration_ms=1000)
    await store.invocation_add(capability="llm:chat", duration_ms=3000)
    await store.invocation_add(capability="image:generate", duration_ms=30000)
    await store.invocation_add(capability="llm:chat", status="error", duration_ms=500)

    stats = {(r["capability"], r["status"]): r for r in await store.invocation_stats(hours=1)}
    ok = stats[("llm:chat", "ok")]
    assert ok["n"] == 2
    assert ok["avg_ms"] == pytest.approx(2000)
    assert stats[("llm:chat", "error")]["n"] == 1


# -- channel_config --------------------------------------------------------- #


async def test_channel_upsert_merges_partial_updates(store: Store) -> None:
    await store.channel_upsert("onebot:group:1", enabled=1, quota_user=5, note="测试群")
    row = await store.channel_get("onebot:group:1")
    assert row is not None
    assert row["enabled"] == 1
    assert row["quota_user"] == 5

    # 只改一个字段，其余保留（POST 一次不该把别的字段清空）
    await store.channel_upsert("onebot:group:1", quota_user=9)
    row = await store.channel_get("onebot:group:1")
    assert row is not None
    assert row["quota_user"] == 9
    assert row["enabled"] == 1
    assert row["note"] == "测试群"


async def test_channel_rejects_unknown_field(store: Store) -> None:
    with pytest.raises(ValueError):
        await store.channel_upsert("onebot:group:1", nope=1)


async def test_channel_list_and_delete(store: Store) -> None:
    await store.channel_upsert("a", note="A")
    await store.channel_upsert("b", note="B")
    assert {r["channel"] for r in await store.channel_all()} == {"a", "b"}
    assert await store.channel_delete("a") is True
    assert await store.channel_delete("a") is False
    assert [r["channel"] for r in await store.channel_all()] == ["b"]


# -- media ------------------------------------------------------------------ #


async def test_media_records_plugin_id_and_filters(store: Store) -> None:
    await store.media_add("m1", "/tmp/a.png", "image/png", 3600, channel="c1", plugin_id="painter")
    await store.media_add("m2", "/tmp/b.png", "image/png", 3600, channel="c1", plugin_id="chat")

    assert {r["id"] for r in await store.media_list()} == {"m1", "m2"}
    only = await store.media_list(plugin_id="painter")
    assert [r["id"] for r in only] == ["m1"]
    assert only[0]["plugin_id"] == "painter"


async def test_media_list_newest_first(store: Store) -> None:
    await store.media_add("old", "/tmp/o.png", "image/png", 3600)
    await store.media_add("new", "/tmp/n.png", "image/png", 3600)
    assert (await store.media_list())[0]["id"] == "new"


# -- 迁移 ------------------------------------------------------------------- #


async def test_migration_adds_plugin_id_to_old_db(tmp_path: Path) -> None:
    """老库没有 media.plugin_id；打开时必须自动补列，否则查询直接炸。"""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE media (id TEXT PRIMARY KEY, path TEXT, mime TEXT, "
        "created_at REAL, expires_at REAL, channel TEXT);"
    )
    con.commit()
    con.close()

    store = await Store(db).open()
    try:
        await store.media_add("m1", "/tmp/a.png", "image/png", 3600, plugin_id="painter")
        rows = await store.media_list(plugin_id="painter")
        assert [r["id"] for r in rows] == ["m1"]
    finally:
        await store.close()

    # 幂等：再开一次不应该报错
    again = await Store(db).open()
    try:
        assert (await again.media_list())[0]["plugin_id"] == "painter"
    finally:
        await again.close()
