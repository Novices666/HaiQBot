"""WebUI API 的公共零件：统一响应、上下文、审计。

响应约定（前端只需要认这一种）：
    成功 → {"ok": true, "data": ...}
    失败 → {"ok": false, "error": {"code": "...", "message": "..."}}
`code` 是给前端做分支的（`unauthorized` / `csrf` / `readonly` / `bad_request` …），
`message` 是给人看的。HTTP 状态码同时给对，方便 curl 调试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from starlette.responses import JSONResponse


@dataclass
class Ctx:
    """一次请求需要的全部依赖。由 `server.build_webui` 构造一次，全局复用。"""

    runtime: Any
    registry: Any
    auth: Any
    config_path: Path
    write_enabled: bool = True

    @property
    def store(self) -> Any:
        return self.runtime.store


def ok(data: Any = None, *, status: int = 200, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"ok": True, "data": data}
    if extra:
        body.update(extra)
    return JSONResponse(body, status_code=status)


def fail(status: int, code: str, message: str, *, hint: str | None = None, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"ok": False, "error": {"code": code, "message": message}}
    if hint:
        payload["error"]["hint"] = hint
    if extra:
        payload.update(extra)
    return JSONResponse(payload, status_code=status)


async def read_json(request: Any) -> dict[str, Any]:
    """读 JSON 对象；不是对象就抛 ValueError（由调用方转 400）。"""
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("请求体不是合法 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return raw


async def audit(ctx: Ctx, action: str, detail: Any, *, actor: str = "webui") -> None:
    """面板自己的操作也要留痕（写配置、启停插件、改白名单…）。"""
    try:
        await ctx.runtime.audit.record(
            trace_id=None,
            plugin_id=None,
            action=f"webui:{action}",
            channel=None,
            actor=actor,
            detail=json.dumps(detail, ensure_ascii=False, default=str)[:800],
        )
    except Exception:  # noqa: BLE001 - 审计失败不该挡住操作结果
        pass


def int_param(value: str | None, default: int, *, low: int, high: int) -> int:
    try:
        return max(low, min(int(value or default), high))
    except (TypeError, ValueError):
        return default


def float_param(value: str | None, default: float, *, low: float, high: float) -> float:
    try:
        return max(low, min(float(value or default), high))
    except (TypeError, ValueError):
        return default
