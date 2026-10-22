"""渠道白名单 / 群维度风控配置。

数据在 SQLite 的 `channel_config` 表里（`kernel/store.py`），
面板只是它的编辑器；写完立刻让 `channel_gate` 失效，避免"改了没生效"。
"""

from __future__ import annotations

from typing import Any

from haiqbot.kernel import channel_control
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json

# 允许从面板改的字段（其余一律拒绝，避免把内部字段写坏）
# 必须与 kernel/store.py 的 _CHANNEL_FIELDS 对齐，否则写进来会被 store 当未知字段拒绝。
ALLOWED_FIELDS = {
    "enabled",
    "plugins",
    "quota_user",
    "quota_chan",
    "extra_admins",
    "note",
}


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime

    async def list_channels(_request: Any) -> Any:
        rows = await runtime.store.channel_all()
        return ok(rows)

    async def upsert(request: Any) -> Any:
        channel = request.path_params["channel"]
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        unknown = set(body) - ALLOWED_FIELDS
        if unknown:
            return fail(
                400,
                "unknown_field",
                f"不认识的字段：{', '.join(sorted(unknown))}",
                hint=f"允许：{', '.join(sorted(ALLOWED_FIELDS))}",
            )
        try:
            await runtime.store.channel_upsert(channel, **body)
        except ValueError as exc:
            return fail(400, "bad_value", str(exc))
        gate = getattr(runtime, "channel_gate", None)
        if gate is not None:
            gate.invalidate(channel)
        row = await runtime.store.channel_get(channel)
        await audit(ctx, "channel.upsert", {"channel": channel, "fields": body})
        return ok(row or {})

    async def remove(request: Any) -> Any:
        channel = request.path_params["channel"]
        deleted = await runtime.store.channel_delete(channel)
        gate = getattr(runtime, "channel_gate", None)
        if gate is not None:
            gate.invalidate(channel)
        await audit(ctx, "channel.delete", {"channel": channel, "ok": deleted})
        return ok({"deleted": deleted})

    async def verify_channels(_request: Any) -> Any:
        """体检：把「配了却不生效」一次说清（插件名错、feature 错、空列表、always 多余）。"""
        problems = await channel_control.verify(runtime, ctx.registry)
        summary = {"error": 0, "warn": 0, "info": 0}
        for item in problems:
            summary[item["level"]] = summary.get(item["level"], 0) + 1
        return ok({"problems": problems, "summary": summary})

    return [
        # 必须排在 /api/channels/{channel} 之前，否则 `verify` 会被当成频道名
        Route("/api/channels/verify", verify_channels, methods=["GET"]),
        Route("/api/channels", list_channels, methods=["GET"]),
        Route("/api/channels/{channel}", upsert, methods=["POST"]),
        Route("/api/channels/{channel}", remove, methods=["DELETE"]),
    ]
