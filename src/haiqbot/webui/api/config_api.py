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


# 接入端字段：改完就地重连，不用重启进程。其余 restart 类字段（webui.* / path.*）办不到。
_ADAPTER_SECTIONS = {"onebot", "official"}


async def _reconnect_if_needed(runtime: Any, result: dict[str, Any]) -> list[str] | None:
    """连接参数变了就重建适配器。返回重建后的接入端列表；没重建返回 None。"""
    paths = [str(change.get("path") or "") for change in result.get("changed", [])]
    if not any(p.split(".", 1)[0] in _ADAPTER_SECTIONS for p in paths):
        return None
    hook = getattr(runtime, "restart_adapters", None)
    if not callable(hook):
        return None
    try:
        platforms = [str(p) for p in await hook()]
    except Exception as exc:  # noqa: BLE001 - 重连失败不该把已落盘的改动一起吞掉
        result.setdefault("warnings", []).append(f"重连接入端失败：{exc}（改动已落盘，重启进程后生效）")
        return None
    warnings = [w for w in result.get("warnings", []) if "需要额外操作" not in str(w)]
    warnings.append(
        "已按新参数重连接入端：" + ("、".join(platforms) if platforms else "无（没有启用的接入端）")
    )
    result["warnings"] = warnings
    return platforms


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime

    async def get_config(_request: Any) -> Any:
        path = Path(ctx.config_path)
        if path.is_dir():
            # 真实踩过的：Docker 在宿主机上找不到 bind mount 的**源文件**时，会自动建一个同名
            # 目录挂进来。此时面板既读不到内容（raw 是空串、**不报错**），写盘又会抛
            # PermissionError（看着像"没权限"）。所以这里必须说清是"路径变成目录了"。
            return fail(
                500,
                "config_path_is_dir",
                f"配置路径是个目录，不是文件：{path}。"
                "宿主机上缺少 haiq.toml / haiq.docker.toml 时，Docker 会自动建一个同名目录挂进来；"
                "compose 的相对路径是按**执行 compose 的目录**解析的，换个目录启动就会这样。"
                "在宿主机建好该文件（或改对 compose 里的路径）后重建容器即可。",
            )
        raw = ""
        warnings: list[str] = []
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError as exc:
                warnings.append(f"配置文件读不出来（{exc.strerror or exc}）：{path}")
        else:
            warnings.append(
                f"配置文件不存在：{path} —— 保存会新建它；"
                "但如果这个目录不在卷里（容器里很常见），重建容器后改动就丢了"
            )
        # 直接摊平 control.describe：前端拿到的就是 fields / secrets / write_enabled
        return ok({**control.describe(runtime), "raw": raw, "path": str(path), "warnings": warnings})

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
        reconnected = await _reconnect_if_needed(runtime, result)
        await audit(
            ctx,
            "config.update",
            {"changed": result, "snapshot": snapshot, "reconnected": reconnected},
        )
        return ok({**result, "snapshot": snapshot, "reconnected": reconnected})

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
