"""定时任务：够用就行的最小实现，不引入 APScheduler。"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any

from haiqbot.kernel.log import get_logger

log = get_logger(__name__)


@dataclass
class _Job:
    name: str
    seconds: float
    fn: Any
    task: asyncio.Task[None] | None = field(default=None)


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, _Job] = {}
        self._running = False

    def add_interval(self, name: str, seconds: float, fn: Any) -> None:
        if seconds <= 0:
            raise ValueError("interval 必须为正数")
        self.remove(name)
        job = _Job(name=name, seconds=float(seconds), fn=fn)
        self._jobs[name] = job
        if self._running:
            job.task = asyncio.create_task(self._loop(job), name=f"haiqbot:{name}")

    def remove(self, name: str) -> None:
        job = self._jobs.pop(name, None)
        if job and job.task:
            job.task.cancel()

    async def start(self) -> None:
        self._running = True
        for job in self._jobs.values():
            if job.task is None:
                job.task = asyncio.create_task(self._loop(job), name=f"haiqbot:{job.name}")

    async def stop(self) -> None:
        self._running = False
        for job in list(self._jobs.values()):
            if job.task:
                job.task.cancel()
        self._jobs.clear()

    async def _loop(self, job: _Job) -> None:
        try:
            while True:
                await asyncio.sleep(job.seconds)
                try:
                    result = job.fn()
                    if inspect.isawaitable(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("scheduled_job_failed", job=job.name)
        except asyncio.CancelledError:
            return
