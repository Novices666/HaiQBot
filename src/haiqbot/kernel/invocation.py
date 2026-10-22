"""能力调用流水（`invocation` 表）。

为什么单独一层
--------------
生图 / 对话是**真花钱**的动作，出了问题要能回答三个问题：
谁在哪个群要了什么、走了哪个 provider/model、花了多久、成没成。
这些信息散落在各个插件里就永远问不出来，所以统一在能力句柄里落一条。

记录策略（与面板「调用日志」页一致）：
- `request` 存**原文**：生图存提示词全文，对话存最后一条用户输入；
- 群号、QQ 号、trace 全记，便于按人/按群排查；
- 永久保留，只在面板上手动按时间窗 / 按插件批量删；
- 落库失败只告警，绝不因为写日志失败影响业务。

密钥红线：这里**只记模型和 provider 名字**，不记任何凭据。
"""

from __future__ import annotations

from typing import Any

from haiqbot.kernel import request_context as rctx
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)

# 单条 request 的落库上限：生图提示词一般几百字，给足余量又不至于把库撑爆
MAX_REQUEST_CHARS = 4000


def _clip(text: Any, limit: int = MAX_REQUEST_CHARS) -> str | None:
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def record(
    store: Any,
    *,
    capability: str,
    plugin_id: str | None = None,
    request: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    duration_ms: int | None = None,
    ok: bool = True,
    error: Any = None,
    detail: str | None = None,
    media_id: str | None = None,
) -> None:
    """落一条调用流水。任何异常都吞掉 —— 审计不能反过来搞挂业务。"""
    if store is None:
        return
    try:
        await store.invocation_add(
            capability=capability,
            plugin_id=plugin_id,
            trace_id=rctx.current_trace(),
            provider=provider,
            model=model,
            request=_clip(request),
            detail=detail,
            duration_ms=duration_ms,
            status="ok" if ok else "failed",
            error=_clip(error, 500),
            channel=rctx.current_channel(),
            actor=rctx.current_actor(),
            media_id=media_id,
        )
    except Exception:  # noqa: BLE001 - 写日志失败不该影响调用结果
        log.warning("invocation_record_failed", capability=capability, plugin=plugin_id)
