"""定时任务接口。

动作故意只有「发文本」和 `noop`（见 kernel/jobs.py 的取舍说明）：
面板可以新建"每天 9 点往某个群发一句话"，但**不能**变成远程执行面板。
"""

from __future__ import annotations

from typing import Any

from haiqbot.kernel.jobs import ACTIONS, KINDS, JobError, JobService
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json


def _service(runtime: Any) -> JobService:
    jobs = getattr(runtime, "jobs", None)
    if jobs is None:
        raise RuntimeError("运行时没有 job 服务")
    return jobs


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime

    async def _channels() -> list[dict[str, Any]]:
        """可选的目标频道（只列已启用的，省得用户挑到静默群）。"""
        try:
            rows = await ctx.store.channel_all()
        except Exception:  # noqa: BLE001 - 列不出就空着，不影响任务管理
            return []
        return [
            {"channel": row["channel"], "note": row.get("note") or ""} for row in rows if row.get("enabled")
        ]

    async def list_all(_request: Any) -> Any:
        try:
            service = _service(runtime)
            items = await service.list()
        except Exception as exc:  # noqa: BLE001
            return fail(500, "jobs_unavailable", f"任务列表读取失败：{type(exc).__name__}: {exc}")
        return ok(
            {
                "items": items,
                "channels": await _channels(),
                "kinds": list(KINDS),
                "actions": list(ACTIONS),
            }
        )

    async def upsert(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        try:
            row = await _service(runtime).upsert(body)
        except JobError as exc:
            return fail(400, "bad_job", str(exc))
        except Exception as exc:  # noqa: BLE001
            return fail(500, "jobs_unavailable", f"保存失败：{type(exc).__name__}: {exc}")
        await audit(
            ctx, "job.save", {"id": row.get("id"), "kind": row.get("kind"), "enabled": row.get("enabled")}
        )
        return ok(row)

    async def toggle(request: Any) -> Any:
        job_id = str(request.path_params.get("job_id") or "")
        enabled = request.url.path.endswith("/enable")
        try:
            row = await _service(runtime).set_enabled(job_id, enabled)
        except JobError as exc:
            return fail(404, "no_such_job", str(exc))
        except Exception as exc:  # noqa: BLE001
            return fail(500, "jobs_unavailable", f"操作失败：{type(exc).__name__}: {exc}")
        await audit(ctx, "job.toggle", {"id": job_id, "enabled": enabled})
        return ok(row)

    async def run_now(request: Any) -> Any:
        job_id = str(request.path_params.get("job_id") or "")
        try:
            result = await _service(runtime).run_now(job_id)
        except JobError as exc:
            await audit(ctx, "job.run", {"id": job_id, "ok": False, "error": str(exc)[:200]})
            return fail(400, "job_run_failed", str(exc))
        except Exception as exc:  # noqa: BLE001
            return fail(500, "jobs_unavailable", f"执行失败：{type(exc).__name__}: {exc}")
        await audit(ctx, "job.run", {"id": job_id, "ok": True})
        return ok(result)

    async def remove(request: Any) -> Any:
        job_id = str(request.path_params.get("job_id") or "")
        try:
            removed = await _service(runtime).delete(job_id)
        except Exception as exc:  # noqa: BLE001
            return fail(500, "jobs_unavailable", f"删除失败：{type(exc).__name__}: {exc}")
        await audit(ctx, "job.delete", {"id": job_id, "removed": removed})
        return ok({"removed": removed})

    return [
        Route("/api/jobs", list_all, methods=["GET"]),
        Route("/api/jobs", upsert, methods=["POST"]),
        Route("/api/jobs/{job_id}/enable", toggle, methods=["POST"]),
        Route("/api/jobs/{job_id}/disable", toggle, methods=["POST"]),
        Route("/api/jobs/{job_id}/run", run_now, methods=["POST"]),
        Route("/api/jobs/{job_id}", remove, methods=["DELETE"]),
    ]
