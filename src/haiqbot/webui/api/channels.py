"""渠道白名单 / 群维度风控配置。

数据在 SQLite 的 `channel_config` 表里（`kernel/store.py`），
面板只是它的编辑器；写完立刻让 `channel_gate` 失效，避免"改了没生效"。
"""

from __future__ import annotations

import asyncio
from typing import Any

from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.kernel import channel_control
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json

#: 问协议端要群/好友列表的总超时：这是一次「打开面板」的交互，不能等太久
_CANDIDATE_TIMEOUT = 10.0

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

    async def candidates(_request: Any) -> Any:
        """「新建 / 开启一个频道」的候选清单（面板上那个可搜索下拉框的数据源）。

        三个来源，任何一个挂了都不影响其它两个：
        1. 适配器 `group_list()` / `friend_list()` —— 最真实（群改名这里就跟着变）；
        2. `channel_config` 里已配置过的 —— 面板要能"接着改"，不能只列新的；
        3. 审计 / 能力流水里**最近出现过**的 —— 协议端没连上时的兜底。

        不合并成一条 SQL 也不缓存：这份数据只在打开面板时用一次，
        缓存的代价（看到过期的群名 / 已退的群）比省下的几毫秒贵。
        """
        items: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        def add(channel: str, label: str, source: str) -> None:
            row = items.get(channel)
            if row is None:
                items[channel] = {"channel": channel, "label": label, "sources": [source]}
                return
            if source not in row["sources"]:
                row["sources"].append(source)
            if not row.get("label") and label:
                row["label"] = label

        for platform, adapter in (getattr(runtime, "adapters", {}) or {}).items():
            for method, kind, id_key, name_keys in (
                ("group_list", "group", "group_id", ("group_name",)),
                ("friend_list", "private", "user_id", ("remark", "nickname")),
            ):
                fetch = getattr(adapter, method, None)
                if not callable(fetch):
                    continue
                try:
                    rows = await asyncio.wait_for(fetch(), timeout=_CANDIDATE_TIMEOUT)
                except Exception as exc:  # noqa: BLE001 - 协议端挂了要在面板上说清
                    errors.append(f"{platform} 取 {method} 失败：{type(exc).__name__}: {exc}")
                    continue
                for row in rows or []:
                    ident = str(row.get(id_key) or "").strip()
                    if not ident:
                        continue
                    name = next((str(row.get(k) or "").strip() for k in name_keys if row.get(k)), "")
                    add(f"{platform}:{kind}:{ident}", f"{name}（{ident}）" if name else ident, "协议端")

        configured = {str(row["channel"]): row for row in await runtime.store.channel_all()}
        for channel in configured:
            add(channel, "", "已配置")
        for row in await runtime.store.seen_channels(limit=100):
            add(str(row["channel"]), "", "最近出现")

        out: list[dict[str, Any]] = []
        for channel, row in items.items():
            try:
                parsed = ChannelId.parse(channel)
            except ValueError:
                continue  # 库里可能有手工写坏的字符串，别拖垮整份列表
            # `onebot:unknown:` 这种（审计里真出现过）没有 id，选了也配不出东西
            if not parsed.id or parsed.kind is ChannelKind.UNKNOWN:
                continue
            existing = configured.get(channel)
            out.append(
                {
                    **row,
                    "platform": parsed.platform,
                    "kind": parsed.kind.value,
                    "id": parsed.id,
                    "configured": existing is not None,
                    "enabled": bool(existing and existing.get("enabled")),
                }
            )
        # 已配置的排前面（面板上最常做的是改已有的），同组按名字排
        out.sort(key=lambda r: (not r["configured"], str(r.get("label") or r["channel"])))
        return ok({"items": out, "errors": errors})

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
        Route("/api/channels/candidates", candidates, methods=["GET"]),
        Route("/api/channels", list_channels, methods=["GET"]),
        Route("/api/channels/{channel}", upsert, methods=["POST"]),
        Route("/api/channels/{channel}", remove, methods=["DELETE"]),
    ]
