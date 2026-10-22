"""面板用的日志环形缓冲。

放在 `kernel/log.py` 里而不是 webui 里，理由：
1. 只有在这里才能保证**先脱敏再入缓冲**（缓冲里的内容天然不含凭据）；
2. 面板、CLI、将来的告警都要看同一份「最近日志」，不该各攒一份。

容量固定、纯内存、进程内 —— 面板要的是「刚刚发生了什么」，不是日志归档。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

_RING_SIZE = 400
_ring: deque[dict[str, Any]] = deque(maxlen=_RING_SIZE)
_seq = 0
_dropped = 0


def ring_capture(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog 处理器：把**已脱敏**的事件记进环形缓冲。

    必须排在 `_redact_processor` 之后 —— 否则缓冲里会出现真值，
    而面板是会把日志原样显示出来的。
    """
    global _seq, _dropped
    _seq += 1
    if len(_ring) == _ring.maxlen:
        _dropped += 1
    item: dict[str, Any] = {
        "seq": _seq,
        "ts": time.time(),
        "level": str(event_dict.get("level", "info")).lower(),
        "event": str(event_dict.get("event", "")),
    }
    extra = {
        k: v for k, v in event_dict.items() if k not in {"level", "event", "timestamp", "logger", "exc_info"}
    }
    if extra:
        item["fields"] = extra
    if event_dict.get("exception"):
        item["exc"] = True
    _ring.append(item)
    return event_dict


def recent_logs(*, since: int = 0, limit: int = 200, level: str | None = None) -> list[dict[str, Any]]:
    """取 `seq > since` 的日志（增量轮询用）。`level` 可过滤最低级别。"""
    order = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
    floor = order.get((level or "").lower(), 0)
    out = [row for row in _ring if row["seq"] > since]
    if floor:
        out = [row for row in out if order.get(row["level"], 20) >= floor]
    return out[-limit:]


def log_cursor() -> dict[str, int]:
    """当前游标：`seq` 是最后一条的序号，`dropped` 是被环形覆盖掉的条数。"""
    return {"seq": _seq, "dropped": _dropped, "size": _ring.maxlen or 0}


def clear_ring() -> None:
    global _seq, _dropped
    _ring.clear()
    _seq = 0
    _dropped = 0


def set_ring_capacity(size: int) -> None:
    """调整容量（测试与低配部署用）。已存在的条目会按新容量截断。"""
    global _ring
    size = max(50, min(int(size), 5000))
    keep = list(_ring)[-size:]
    _ring = deque(keep, maxlen=size)
