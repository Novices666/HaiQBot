"""配置中心：查看、热改、历史、回滚。

三条底线：
1. **密钥真值永不经过面板** —— 只展示 `*_ref` 键名，改值请去 `haiq secrets`；
2. 每次写之前先落一份快照（`data/webui/config-history/`），回滚不需要 git；
3. 写操作复用 `kernel/control.apply_changes`（与 CLI 同一套校验），面板不自己拼 TOML。
"""

from __future__ import annotations

import time
import tomllib
from pathlib import Path
from typing import Any

from haiqbot.kernel import control
from haiqbot.webui.api.common import Ctx, audit, fail, int_param, ok, read_json

_MAX_SNAPSHOTS = 30


def _history_dir(ctx: Ctx) -> Path:
    base = Path(getattr(ctx.runtime.config, "data_dir", "data")) / "webui" / "config-history"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _snapshot(ctx: Ctx) -> str | None:
    path = Path(ctx.config_path)
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = _history_dir(ctx) / f"{stamp}-{int(time.time() * 1000) % 1000:03d}.toml"
    try:
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:
        return None
    # 只留最近 N 份，避免长期跑下来攒成垃圾堆
    files = sorted(_history_dir(ctx).glob("*.toml"))
    for old in files[:-_MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)
    return target.name


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime

    async def get_config(_request: Any) -> Any:
        path = Path(ctx.config_path)
        raw = ""
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                raw = ""
        # 直接摊平 control.describe：前端拿到的就是 fields / secrets / write_enabled
        return ok({**control.describe(runtime), "raw": raw, "path": str(path)})

    async def post_config(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        snapshot = _snapshot(ctx)
        try:
            result = control.apply_changes(runtime, body)
        except control.ControlError as exc:
            return fail(exc.status_code, "control_error", exc.message, snapshot=snapshot)
        await audit(ctx, "config.update", {"changed": result, "snapshot": snapshot})
        return ok({**result, "snapshot": snapshot})

    async def history(_request: Any) -> Any:
        rows = []
        for path in sorted(_history_dir(ctx).glob("*.toml"), reverse=True):
            stat = path.stat()
            rows.append({"id": path.name, "ts": stat.st_mtime, "size": stat.st_size})
        return ok(rows)

    async def rollback(request: Any) -> Any:
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        snapshot_id = str(body.get("id") or "")
        if not snapshot_id or Path(snapshot_id).name != snapshot_id:
            return fail(400, "bad_request", "快照 id 不合法")
        source = _history_dir(ctx) / snapshot_id
        if not source.is_file():
            return fail(404, "not_found", f"没有快照 {snapshot_id}")
        text = source.read_text(encoding="utf-8")
        try:
            tomllib.loads(text)  # 先确认这份快照能解析，别把面板写进死配置
        except tomllib.TOMLDecodeError as exc:
            return fail(400, "invalid_snapshot", f"快照本身不是合法 TOML：{exc}")
        _snapshot(ctx)  # 回滚也可回滚
        Path(ctx.config_path).write_text(text, encoding="utf-8")
        await audit(ctx, "config.rollback", {"id": snapshot_id})
        return ok(
            {
                "restored": snapshot_id,
                "note": "文件已还原；插件/日志级别等改动通常需要重启 haiqbot 才完全生效",
            }
        )

    async def raw(request: Any) -> Any:
        """取原始 TOML 文本（前端做 diff / 只读预览用）。"""
        path = Path(ctx.config_path)
        if not path.is_file():
            return fail(404, "not_found", "配置文件不存在")
        limit = int_param(request.query_params.get("limit"), 200_000, low=1000, high=2_000_000)
        text = path.read_text(encoding="utf-8")[:limit]
        return ok({"path": str(path), "text": text})

    return [
        Route("/api/config", get_config, methods=["GET"]),
        Route("/api/config", post_config, methods=["POST"]),
        Route("/api/config/raw", raw, methods=["GET"]),
        Route("/api/config/history", history, methods=["GET"]),
        Route("/api/config/rollback", rollback, methods=["POST"]),
    ]
