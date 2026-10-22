"""定时任务：调度计算、校验、执行、持久化。

重点不是「能跑」，而是：
1. 校验层把不该跑的挡掉（间隔太小的刷屏任务、非法频道）；
2. 单次失败不能让后续循环停摆（next_run_at 必须继续前进）；
3. 重启后节奏不丢、不重跑。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from haiqbot.core.channel import ChannelId
from haiqbot.kernel.jobs import JobError, JobService, next_run, validate
from haiqbot.kernel.store import Store


class FakeAdapter:
    platform = "onebot"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None

    async def send_text(self, channel: ChannelId, text: str, **kwargs: Any) -> str | None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append({"channel": str(channel), "text": text, "plugin_id": kwargs.get("plugin_id")})
        return "msg-1"


class FakeRuntime:
    def __init__(self, adapter: FakeAdapter | None = None) -> None:
        self.adapters = {"onebot": adapter or FakeAdapter()}


def _svc(store: Store, adapter: FakeAdapter | None = None) -> JobService:
    return JobService(store, FakeRuntime(adapter))


# --------------------------------------------------------------------------- #
# 调度计算
# --------------------------------------------------------------------------- #


def test_next_run_interval() -> None:
    assert next_run("interval", {"seconds": 60}, now=1000.0) == 1060.0


def test_next_run_daily_today_or_tomorrow() -> None:
    # 2024-01-01 08:00 UTC 附近的本地时间不重要，只要「比现在晚且不超过 24h」
    now = time.time()
    nxt = next_run("daily", {"at": "09:30"}, now=now)
    assert now < nxt <= now + 86400 + 60


def test_unknown_kind_rejected() -> None:
    with pytest.raises(JobError):
        next_run("cron", {"at": "0 9 * * *"}, now=time.time())


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def test_validate_defaults_and_id() -> None:
    row = validate(
        {
            "name": "早报",
            "kind": "daily",
            "schedule": {"at": "09:30"},
            "params": {"channel": "onebot:group:1", "text": "hi"},
        }
    )
    assert row["kind"] == "daily"
    assert row["id"].startswith("job_")
    assert row["enabled"] is True


def test_interval_too_small_rejected() -> None:
    with pytest.raises(JobError, match="不能小于"):
        validate({"name": "刷屏", "kind": "interval", "schedule": {"seconds": 1}, "params": {}})


def test_daily_needs_hh_mm() -> None:
    with pytest.raises(JobError):
        validate({"name": "x", "kind": "daily", "schedule": {"at": "9点半"}, "params": {}})


def test_send_text_needs_valid_channel_and_text() -> None:
    base = {"name": "x", "kind": "interval", "schedule": {"seconds": 60}, "action": "send_text"}
    with pytest.raises(JobError, match="channel"):
        validate({**base, "params": {"channel": "bad", "text": "hi"}})
    with pytest.raises(JobError, match="text"):
        validate({**base, "params": {"channel": "onebot:group:1", "text": "  "}})


def test_unknown_action_rejected() -> None:
    """动作白名单是硬边界：不许变成「定时执行任意命令」。"""
    with pytest.raises(JobError, match="action"):
        validate({"name": "x", "action": "exec_shell", "params": {}})


def test_bad_id_characters_rejected() -> None:
    with pytest.raises(JobError):
        validate({"id": "job/../x", "name": "x", "params": {}})


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


async def test_tick_sends_text_and_advances(store: Store) -> None:
    adapter = FakeAdapter()
    service = _svc(store, adapter)
    row = await service.upsert(
        {
            "name": "测试播报",
            "kind": "interval",
            "schedule": {"seconds": 60},
            "action": "send_text",
            "params": {"channel": "onebot:group:674425800", "text": "早上好"},
        }
    )
    # 把下次执行时间挪到过去，模拟「到点了」
    await store.job_mark_run(row["id"], status="pending", error=None, next_run_at=time.time() - 1, ran_at=0.0)

    assert await service.tick() == 1
    assert adapter.sent == [{"channel": "onebot:group:674425800", "text": "早上好", "plugin_id": "core:job"}]

    after = await store.job_get(row["id"])
    assert after is not None
    assert after["last_status"] == "ok"
    assert after["next_run_at"] > time.time(), "跑完必须排下一次"

    # 再 tick 一次不应该重复执行
    assert await service.tick() == 0


async def test_failure_is_recorded_and_loop_continues(store: Store) -> None:
    adapter = FakeAdapter()
    adapter.fail_with = RuntimeError("平台挂了")
    service = _svc(store, adapter)
    row = await service.upsert(
        {
            "name": "会失败",
            "kind": "interval",
            "schedule": {"seconds": 60},
            "params": {"channel": "onebot:group:1", "text": "x"},
        }
    )
    await store.job_mark_run(row["id"], status="pending", error=None, next_run_at=time.time() - 1, ran_at=0.0)

    assert await service.tick() == 1
    after = await store.job_get(row["id"])
    assert after is not None
    assert after["last_status"] == "error"
    assert "平台挂了" in (after["last_error"] or "")
    assert after["next_run_at"] > time.time(), "失败也要排下一次，否则任务就死了"


async def test_disabled_job_is_not_run(store: Store) -> None:
    adapter = FakeAdapter()
    service = _svc(store, adapter)
    row = await service.upsert(
        {
            "name": "停用的",
            "kind": "interval",
            "schedule": {"seconds": 60},
            "params": {"channel": "onebot:group:1", "text": "x"},
        }
    )
    await service.set_enabled(row["id"], False)
    await store.job_mark_run(row["id"], status="pending", error=None, next_run_at=time.time() - 1, ran_at=0.0)

    assert await service.tick() == 0
    assert adapter.sent == []


async def test_run_now_does_not_shift_schedule(store: Store) -> None:
    adapter = FakeAdapter()
    service = _svc(store, adapter)
    row = await service.upsert(
        {
            "name": "手动跑",
            "kind": "interval",
            "schedule": {"seconds": 600},
            "params": {"channel": "onebot:group:1", "text": "hey"},
        }
    )
    before = (await store.job_get(row["id"]))["next_run_at"]

    await service.run_now(row["id"])

    after = await store.job_get(row["id"])
    assert adapter.sent and adapter.sent[0]["text"] == "hey"
    assert after["next_run_at"] == before, "手动执行不该打乱既定节奏"


async def test_missing_adapter_is_a_recorded_error(store: Store) -> None:
    service = JobService(store, FakeRuntime())  # 只有 onebot
    row = await service.upsert(
        {
            "name": "发到不存在的平台",
            "kind": "interval",
            "schedule": {"seconds": 60},
            "params": {"channel": "official:group:1", "text": "x"},
        }
    )
    await store.job_mark_run(row["id"], status="pending", error=None, next_run_at=time.time() - 1, ran_at=0.0)
    await service.tick()

    after = await store.job_get(row["id"])
    assert after is not None
    assert after["last_status"] == "error"
    assert "official" in (after["last_error"] or "")


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #


async def test_jobs_survive_new_service_instance(tmp_path: Path) -> None:
    db = tmp_path / "j.db"
    store = await Store(db).open()
    first = JobService(store, FakeRuntime())
    created = await first.upsert(
        {
            "name": "持久化",
            "kind": "daily",
            "schedule": {"at": "09:30"},
            "params": {"channel": "onebot:group:1", "text": "早"},
        }
    )
    await store.close()

    reopened = await Store(db).open()
    try:
        second = JobService(reopened, FakeRuntime())
        items = await second.list()
        assert [row["id"] for row in items] == [created["id"]]
        assert items[0]["schedule"] == {"at": "09:30"}
        assert items[0]["params"]["text"] == "早"
    finally:
        await reopened.close()


async def test_update_keeps_last_run_history(store: Store) -> None:
    service = _svc(store)
    row = await service.upsert(
        {
            "name": "原名",
            "kind": "interval",
            "schedule": {"seconds": 60},
            "params": {"channel": "onebot:group:1", "text": "a"},
        }
    )
    await store.job_mark_run(row["id"], status="ok", error=None, next_run_at=time.time() + 60, ran_at=12345.0)

    updated = await service.upsert({"id": row["id"], "name": "改名"})
    assert updated["name"] == "改名"
    assert updated["last_run_at"] == 12345.0, "改配置不该抹掉执行历史"
