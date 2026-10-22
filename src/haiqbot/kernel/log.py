"""结构化日志 + 凭据脱敏。

这是「密钥不进日志」那道闸的实现：
1. 静态模式匹配已知形态（`sk-…`、`Bearer …`、`clientSecret: …`）；
2. 运行时注册：vault 每取出一个真值就 `register_secret()`，日志里出现即被打码。
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from haiqbot.kernel.log_ring import ring_capture

REDACTED = "***REDACTED***"

# 已知凭据形态
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)bot\s+[A-Za-z0-9._\-]{16,}"),
]

# key: value 形态，保留键名、掩掉值
_KV_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|client[_-]?secret|app[_-]?secret|access[_-]?token|secret|password|passwd)"
    r"([\"']?)(\s*[:=]\s*)([\"']?)([^\s\"',;}]{4,})\4"
)


def _kv_repl(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}{REDACTED}{m.group(4)}"


# 这些字段名一旦出现在日志字典里，整个值直接打码
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "client_secret",
    "clientsecret",
    "app_secret",
    "appsecret",
    "access_token",
    "accesstoken",
    "password",
    "passwd",
    "authorization",
    "token",
}

# 运行时注册的真值（vault 取出即登记）
_registered: set[str] = set()


def register_secret(value: str | None) -> None:
    """把一个真值登记到脱敏名单，之后任何日志中出现都会被替换。"""
    if value and len(value) >= 4:
        _registered.add(value)


def clear_registered_secrets() -> None:
    _registered.clear()


def redact_text(text: str) -> str:
    if not text:
        return text
    out = text
    # 先按长度倒序替换精确真值，避免短串先把长串切碎
    for secret in sorted(_registered, key=len, reverse=True):
        out = out.replace(secret, REDACTED)
    for pat in _PATTERNS:
        out = pat.sub(REDACTED, out)
    out = _KV_PATTERN.sub(_kv_repl, out)
    return out


def redact_value(value: Any, _depth: int = 0) -> Any:
    """递归脱敏任意结构。"""
    if _depth > 8:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, MutableMapping):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower().replace("-", "_") in _SENSITIVE_KEYS:
                out[k] = REDACTED
            else:
                out[k] = redact_value(v, _depth + 1)
        return out
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v, _depth + 1) for v in value)
    return value


def _redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return redact_value(dict(event_dict))


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=lvl, stream=sys.stderr, format="%(message)s")

    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.TimeStamper(fmt="iso", utc=False),
            # 脱敏必须在入环形缓冲之前 —— 面板会把日志原样显示出来
            _redact_processor,
            ring_capture,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)
