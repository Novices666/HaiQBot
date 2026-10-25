"""定时任务：持久化 + 两种调度 + 一个安全动作。

设计取舍（改之前先想清楚为什么要加新东西）：
- **只支持 `interval` 和 `daily`**，不引入 cron 表达式依赖 ——「每天 9 点播报」用
  `daily` 就够，多一种语法就多一处能配错的地方。
- **动作只允许「发一段文本到某个频道」**（`send_text`，另有 `noop` 用于自检）。
  刻意不做「定时执行任意命令」—— 那样面板就成了远程执行面板。
- 调度状态（`next_run_at` / `last_run_at` / `last_status`）落库，重启不丢、不重跑；
  同一任务同一时刻只执行一次（由 tick 串行保证）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from .log import get_logger

log = get_logger(__name__)

KINDS = ("interval", "daily")
ACTIONS = ("send_text", "noop")
MIN_INTERVAL_SECONDS = 5.0
TICK_SECONDS = 15.0
MAX_TEXT_CHARS = 2000
_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")


class JobError(ValueError):
    """任务定义不合法。消息直接给人和面板看。"""


def _next_interval(seconds: float, now: float) -> float:
    return now + seconds


def _next_daily(at: str, now: float) -> float:
    try:
        hh_text, mm_text = str(at).split(":")
        hour, minute = int(hh_text), int(mm_text)
    except (ValueError, AttributeError) as exc:
        raise JobError(f"daily 的 at 必须形如 09:30，收到 {at!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise JobError(f"daily 的 at 不合法：{at!r}")
    current = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current.timestamp() <= now:
        current += timedelta(days=1)
    return current.timestamp()


def next_run(kind: str, schedule: dict[str, Any], now: float | None = None) -> float:
    """下一次该跑的时刻（epoch 秒）。"""
    moment = time.time() if now is None else now
    if kind == "interval":
        return _next_interval(float(schedule["seconds"]), moment)
    if kind == "daily":
        return _next_daily(str(schedule.get("at", "")), moment)
    raise JobError(f"未知的调度类型：{kind}（只支持 {' / '.join(KINDS)}）")


def _check_schedule(kind: str, schedule: Any) -> dict[str, Any]:
    if not isinstance(schedule, dict):
        raise JobError('schedule 必须是对象，例如 {"seconds": 600} 或 {"at": "09:30"}')
    if kind == "interval":
        try:
            seconds = float(schedule.get("seconds"))
        except (TypeError, ValueError) as exc:
            raise JobError("interval 需要数字 seconds") from exc
        if seconds < MIN_INTERVAL_SECONDS:
            raise JobError(f"interval 不能小于 {MIN_INTERVAL_SECONDS:.0f} 秒（太小会刷屏/触发风控）")
        return {"seconds": seconds}
    at = str(schedule.get("at", "")).strip()
    _next_daily(at, time.time())  # 校验格式
    return {"at": at}


def _check_params(action: str, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise JobError("params 必须是对象")
    if action == "noop":
        return {}
    channel = str(params.get("channel", "")).strip()
    text = str(params.get("text", ""))
    if not channel:
        raise JobError("send_text 需要 params.channel，例如 onebot:group:200000001")
    from haiqbot.core.channel import ChannelId

    try:
        ChannelId.parse(channel)
    except ValueError as exc:
        raise JobError(f"channel 不合法：{channel}（应形如 platform:kind:id）") from exc
    if not text.strip():
        raise JobError("send_text 需要非空的 params.text")
    if len(text) > MAX_TEXT_CHARS:
        raise JobError(f"文本太长（{len(text)} > {MAX_TEXT_CHARS} 字）")
    return {"channel": channel, "text": text}


def validate(payload: dict[str, Any], *, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """归一化 + 校验，返回可直接落库的字段。"""
    if not isinstance(payload, dict):
        raise JobError("任务定义必须是对象")

    job_id = str(payload.get("id") or (existing or {}).get("id") or f"job_{uuid.uuid4().hex[:10]}").strip()
    if not job_id or set(job_id) - _ID_CHARS:
        raise JobError("id 只能用字母数字和 _ . : -")

    name = str(payload.get("name") or (existing or {}).get("name") or "").strip()
    if not name:
        raise JobError("name 不能为空")

    kind = str(payload.get("kind") or (existing or {}).get("kind") or "interval").strip()
    if kind not in KINDS:
        raise JobError(f"kind 只能是 {' / '.join(KINDS)}")

    action = str(payload.get("action") or (existing or {}).get("action") or "send_text").strip()
    if action not in ACTIONS:
        raise JobError(f"action 只能是 {' / '.join(ACTIONS)}")

    schedule = _check_schedule(kind, payload.get("schedule", (existing or {}).get("schedule")))
    params = _check_params(action, payload.get("params", (existing or {}).get("params") or {}))

    enabled = payload.get("enabled", (existing or {}).get("enabled", True))
    return {
        "id": job_id,
        "name": name,
        "kind": kind,
        "schedule": schedule,
        "action": action,
        "params": params,
        "enabled": bool(enabled),
    }


class JobService:
    """从 store 读任务、按到期时间执行、把结果写回 store。"""

    def __init__(self, store: Any, runtime: Any) -> None:
        self.store = store
        self.runtime = runtime
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # -- 查询/编辑 ------------------------------------------------------- #
    async def list(self) -> list[dict[str, Any]]:
        rows = await self.store.job_all()
        return [await self._present(row) for row in rows]

    async def get(self, job_id: str) -> dict[str, Any] | None:
        row = await self.store.job_get(job_id)
        return await self._present(row) if row else None

    @staticmethod
    async def _present(row: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        out = dict(row)
        out["due_in"] = round(row["next_run_at"] - now, 1) if row.get("next_run_at") else None
        return out

    async def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("id") or "").strip()
        existing = await self.store.job_get(job_id) if job_id else None
        fields = validate(payload, existing=existing)
        now = time.time()
        fields["next_run_at"] = (
            next_run(fields["kind"], fields["schedule"], now) if fields["enabled"] else None
        )
        if existing is not None:
            fields["last_run_at"] = existing.get("last_run_at")
            fields["last_status"] = existing.get("last_status")
            fields["last_error"] = existing.get("last_error")
            fields["created_at"] = existing.get("created_at") or now
        else:
            fields["last_run_at"] = None
            fields["last_status"] = None
            fields["last_error"] = None
            fields["created_at"] = now
        await self.store.job_upsert(**fields)
        log.info("job_saved", job=fields["id"], kind=fields["kind"], enabled=fields["enabled"])
        return (await self.get(fields["id"])) or {}

    async def set_enabled(self, job_id: str, enabled: bool) -> dict[str, Any]:
        row = await self.store.job_get(job_id)
        if row is None:
            raise JobError(f"没有这个任务：{job_id}")
        return await self.upsert({**row, "enabled": enabled})

    async def delete(self, job_id: str) -> bool:
        return await self.store.job_delete(job_id)

    async def run_now(self, job_id: str) -> dict[str, Any]:
        """立刻执行一次，但**不改** next_run_at（避免打乱既定节奏）。"""
        row = await self.store.job_get(job_id)
        if row is None:
            raise JobError(f"没有这个任务：{job_id}")
        ok, error = await self._run_row(row, time.time(), keep_schedule=True)
        result = {"ok": ok, "error": error}
        if not ok:
            raise JobError(f"执行失败：{error}")
        return result

    # -- 执行 ------------------------------------------------------------ #
    async def _execute(self, row: dict[str, Any]) -> None:
        action = row.get("action")
        params = row.get("params") or {}
        if action == "noop":
            return
        if action != "send_text":  # 校验层已挡，这里防手改库
            raise JobError(f"未知动作：{action}")

        from haiqbot.core.channel import ChannelId

        channel = ChannelId.parse(str(params.get("channel") or ""))
        adapters = getattr(self.runtime, "adapters", None) or {}
        adapter = adapters.get(channel.platform)
        if adapter is None:
            raise JobError(f"没有可用的 {channel.platform} 适配器（当前：{sorted(adapters)}）")
        await adapter.send_text(channel, str(params.get("text") or ""), plugin_id="core:job")

    async def _run_row(
        self, row: dict[str, Any], moment: float, *, keep_schedule: bool = False
    ) -> tuple[bool, str | None]:
        ok, error = False, None
        try:
            await self._execute(row)
            ok = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单任务失败不该影响其它任务
            error = f"{type(exc).__name__}: {exc}"

        next_at: float | None = row.get("next_run_at")
        if not keep_schedule:
            try:
                next_at = next_run(row.get("kind", ""), row.get("schedule") or {}, time.time())
            except (JobError, KeyError, TypeError):
                next_at = None

        await self.store.job_mark_run(
            str(row["id"]),
            status="ok" if ok else "error",
            error=(error or "")[:300] or None,
            next_run_at=next_at,
            ran_at=moment,
        )
        log.info("job_ran", job=row.get("id"), ok=ok, error=error, next_run_at=next_at)
        return ok, error

    async def tick(self, now: float | None = None) -> int:
        """执行所有到期任务。返回执行条数（测试直接调它，不用等循环）。"""
        moment = time.time() if now is None else now
        ran = 0
        for row in await self.store.job_due(moment):
            await self._run_row(row, moment)
            ran += 1
        return ran

    # -- 循环 ------------------------------------------------------------ #
    async def _backfill(self) -> None:
        """启用着但没有下次执行时间的任务（老库/手改库）补一个。"""
        now = time.time()
        for row in await self.store.job_all():
            if not row.get("enabled") or row.get("next_run_at"):
                continue
            try:
                nxt = next_run(row.get("kind", ""), row.get("schedule") or {}, now)
            except (JobError, KeyError, TypeError):
                continue
            await self.store.job_mark_run(
                str(row["id"]),
                status=row.get("last_status") or "pending",
                error=row.get("last_error"),
                next_run_at=nxt,
                ran_at=row.get("last_run_at") or 0.0,
            )

    async def start(self) -> None:
        if self._task is not None:
            return
        await self._backfill()
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="haiqbot:jobs")
        log.info("job_service_started", tick_seconds=TICK_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 循环不能因一次异常而死
                log.exception("job_tick_failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                continue
            else:
                return
