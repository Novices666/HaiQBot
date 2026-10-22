"""可观测性：总览、日志、审计、调用流水。

面板的「轮询」全部落在这一组只读接口上 —— 它们只读内存计数器和 SQLite，
不做任何副作用，所以前端可以放心 1~3 秒拉一次。
"""

from __future__ import annotations

import time
from typing import Any

from haiqbot.kernel import log_ring
from haiqbot.webui.api.common import Ctx, audit, fail, float_param, int_param, ok, read_json

# SSE 连接计数：每个打开的标签页占一条。上限是防「标签页开太多把服务拖住」。
_STREAMS: list[int] = [0]
MAX_LOG_STREAMS = 6


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime
    registry = ctx.registry

    # -- 总览 ---------------------------------------------------------------- #
    async def overview(_request: Any) -> Any:
        status = registry.status() if registry is not None else []
        loaded = [row for row in status if row.get("loaded")]
        blocked = registry.blocked() if registry is not None and hasattr(registry, "blocked") else {}
        errors = registry.status_errors if registry is not None else {}

        health = runtime.health()
        media_count = 0
        try:
            media_count = await runtime.store.media_count()
        except Exception:  # noqa: BLE001 - 面板不能因为统计失败整体挂掉
            media_count = 0

        audit_rows: list[dict[str, Any]] = []
        try:
            audit_rows = await runtime.store.recent_audit(5)
        except Exception:  # noqa: BLE001
            audit_rows = []

        outbound = getattr(runtime, "outbound", None)
        return ok(
            {
                "runtime": {
                    "started_at": runtime.started_at,
                    "uptime_seconds": round(time.time() - runtime.started_at, 1),
                    "events_total": runtime.events_total,
                    "handled_total": runtime.handled_total,
                    "last_event_ts": runtime.last_event_ts,
                },
                "health": health,
                "platforms": {name: adapter.describe() for name, adapter in runtime.adapters.items()},
                "plugins": {
                    "total": len(status),
                    "loaded": len(loaded),
                    "blocked": sorted(blocked) if isinstance(blocked, dict | set) else list(blocked or []),
                    "errors": errors,
                },
                "media": {"count": media_count},
                "outbound": (
                    {
                        "min_interval_seconds": getattr(outbound, "min_interval", None),
                        "global_per_minute": getattr(outbound, "global_per_minute", None),
                    }
                    if outbound is not None
                    else None
                ),
                "config": {
                    "onebot_mode": getattr(runtime.config.onebot, "mode", None),
                    "onebot_enabled": runtime.config.onebot.enabled,
                    "official_enabled": runtime.config.official.enabled,
                    "quota_enabled": runtime.config.quota.enabled,
                    "log_level": runtime.config.log_level,
                    "write_enabled": ctx.write_enabled,
                },
                "recent_audit": audit_rows,
            }
        )

    # -- 日志（增量轮询） ---------------------------------------------------- #
    async def logs(request: Any) -> Any:
        q = request.query_params
        since = int_param(q.get("since"), 0, low=0, high=2**31)
        limit = int_param(q.get("limit"), 200, low=1, high=800)
        level = (q.get("level") or "").strip() or None
        items = log_ring.recent_logs(since=since, limit=limit, level=level)
        return ok({"items": items, "cursor": log_ring.log_cursor()})

    # -- 日志（SSE 实时推送） ------------------------------------------------ #
    async def logs_stream(request: Any) -> Any:
        """增量推送新日志；`since` 接上次的 cursor.seq。

        为什么用 SSE 而不是 WebSocket：只需要服务端单向推、走 GET（天然免 CSRF）、
        浏览器断线会自动重连 —— 这里要的正好是这三条。
        """
        import asyncio
        import json

        from starlette.responses import StreamingResponse

        q = request.query_params
        since = int_param(q.get("since"), 0, low=0, high=2**31)
        level = (q.get("level") or "").strip() or None
        if _STREAMS[0] >= MAX_LOG_STREAMS:
            return fail(429, "too_many_streams", "实时日志连接太多了，先关掉几个标签页")
        _STREAMS[0] += 1

        async def gen() -> Any:
            cursor = since
            idle = 0
            try:
                yield f"event: ready\ndata: {json.dumps({'since': cursor, 'level': level})}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    rows = log_ring.recent_logs(since=cursor, limit=200, level=level)
                    if rows:
                        cursor = int(rows[-1]["seq"])
                        yield "data: " + json.dumps({"items": rows}, ensure_ascii=False) + "\n\n"
                        idle = 0
                    else:
                        idle += 1
                        if idle >= 15:
                            yield ": ping\n\n"  # 心跳，防反代把空闲连接掐掉
                            idle = 0
                    await asyncio.sleep(1.0)
            finally:
                _STREAMS[0] -= 1

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"cache-control": "no-store", "x-accel-buffering": "no"},
        )

    # -- 审计 ---------------------------------------------------------------- #
    async def audit_list(request: Any) -> Any:
        limit = int_param(request.query_params.get("limit"), 200, low=1, high=500)
        rows = await runtime.store.recent_audit(limit)
        return ok(rows)

    # -- 调用流水 / 配额统计 -------------------------------------------------- #
    async def invocations(request: Any) -> Any:
        q = request.query_params
        limit = int_param(q.get("limit"), 100, low=1, high=500)
        rows = await runtime.store.invocation_recent(
            limit,
            plugin_id=q.get("plugin") or None,
            capability=q.get("capability") or None,
        )
        return ok(rows)

    async def invocation_stats(request: Any) -> Any:
        hours = float_param(request.query_params.get("hours"), 24.0, low=1.0, high=720.0)
        return ok(await runtime.store.invocation_stats(hours=hours))

    async def invocation_delete(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        try:
            deleted = await runtime.store.invocation_delete(
                before=body.get("before"),
                plugin_id=body.get("plugin_id") or None,
                capability=body.get("capability") or None,
            )
        except ValueError as exc:  # 不允许无条件清空整表
            return fail(400, "bad_filter", str(exc))
        await audit(ctx, "invocation.delete", {"deleted": deleted, "filter": body})
        return ok({"deleted": deleted})

    return [
        Route("/api/overview", overview, methods=["GET"]),
        Route("/api/logs", logs, methods=["GET"]),
        Route("/api/logs/stream", logs_stream, methods=["GET"]),
        Route("/api/audit", audit_list, methods=["GET"]),
        Route("/api/invocations", invocations, methods=["GET"]),
        Route("/api/invocations/stats", invocation_stats, methods=["GET"]),
        Route("/api/invocations/delete", invocation_delete, methods=["POST"]),
    ]
