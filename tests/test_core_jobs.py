"""内核自排的维护任务：媒体 GC 必须真的被排上，否则 ttl 形同虚设。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from haiqbot.app import DEDUPE_GC_SECONDS, App
from haiqbot.config import HaiqConfig, MediaCfg
from haiqbot.services.media_store import MediaStore
from haiqbot.services.scheduler import Scheduler


class FakeStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    async def media_add(self, media_id, path, mime, ttl, channel=None, plugin_id=None) -> None:
        self.rows.append({"id": media_id, "path": path, "mime": mime})

    async def media_expired(self) -> list[dict]:
        return list(self.rows)

    async def media_delete(self, media_id: str) -> bool:
        self.rows = [row for row in self.rows if row["id"] != media_id]
        return True


@pytest.mark.asyncio
async def test_media_gc_removes_file_and_row(tmp_path):
    store = FakeStore()
    media = MediaStore(store, MediaCfg(dir=tmp_path))
    ref = await media.put(b"png-bytes", "image/png")
    assert ref.path.is_file()

    removed = await media.gc()
    assert removed == 1
    assert not ref.path.exists()
    assert store.rows == []


def test_core_job_is_registered():
    """用 SimpleNamespace 直接跑未绑定方法：只依赖 self.runtime / self.config。"""
    scheduler = Scheduler()
    media = SimpleNamespace(gc=lambda: None)
    fake_self = SimpleNamespace(
        runtime=SimpleNamespace(scheduler=scheduler, media=media),
        config=HaiqConfig(),
        store=None,
    )
    App._register_core_jobs(fake_self)
    assert "core:media-gc" in scheduler._jobs
    assert scheduler._jobs["core:media-gc"].seconds == float(HaiqConfig().media.gc_interval_seconds)


def test_core_job_skipped_when_interval_zero():
    scheduler = Scheduler()
    fake_self = SimpleNamespace(
        runtime=SimpleNamespace(scheduler=scheduler, media=SimpleNamespace(gc=lambda: None)),
        config=HaiqConfig(media=MediaCfg(gc_interval_seconds=0)),
        store=None,
    )
    App._register_core_jobs(fake_self)
    assert scheduler._jobs == {}


def test_dedupe_gc_registered_even_when_media_gc_off():
    """去重表必须自己有人清：关掉媒体 GC 不该让它无限增长。"""
    scheduler = Scheduler()
    fake_self = SimpleNamespace(
        runtime=SimpleNamespace(scheduler=scheduler, media=SimpleNamespace(gc=lambda: None)),
        config=HaiqConfig(media=MediaCfg(gc_interval_seconds=0)),
        store=SimpleNamespace(gc_dedupe=lambda: None),
    )
    App._register_core_jobs(fake_self)
    assert scheduler._jobs["core:dedupe-gc"].seconds == DEDUPE_GC_SECONDS
    assert "core:media-gc" not in scheduler._jobs
