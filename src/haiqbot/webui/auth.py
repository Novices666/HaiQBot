"""面板认证：单令牌 + 无状态会话 Cookie + CSRF 双保险。

为什么不用服务端会话表：
- 面板是单用户工具，重启 haiqbot 后会话失效并不可惜；
- Cookie 里放 `v1.<签发时间>.<签名>`，签名密钥由令牌派生 —— **换令牌 = 踢掉所有会话**。
  没有可被遗忘的会话表，也就没有"退出登录但服务端还记着"的坑。

为什么还要 CSRF：Cookie 是 SameSite=Lax，跨站 POST 已经拿不到；
但反代/自定义域下 Lax 未必可靠，所以写操作额外要求 `X-Haiq-CSRF` 头（值与会话绑定）。

登录限速：按来源 IP 记失败次数，60 秒内超过 5 次就 429 —— 面板通常只听 127.0.0.1，
但反代暴露到内网时这一层仍然有用。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

COOKIE_NAME = "haiq_session"
_SESSION_VERSION = "v1"
_SALT_SESSION = b"haiqbot-webui-session-v1"
_SALT_CSRF = b"haiqbot-webui-csrf-v1"

DEFAULT_TTL_SECONDS = 12 * 3600
MAX_FAILURES = 5
FAILURE_WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class Session:
    issued_at: int
    csrf: str

    @property
    def expires_at(self) -> int:
        return self.issued_at + DEFAULT_TTL_SECONDS


class Auth:
    """令牌校验 + 会话签发/验证 + 登录限速。"""

    def __init__(
        self,
        *,
        token: str | None = None,
        token_provider: Callable[[], str | None] | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        # 令牌**每次现取**：haiq webui token --generate 换完立即生效，
        # 不需要重启 haiqbot；同时旧会话会因为签名密钥变了而自动失效。
        if token_provider is not None:
            self._provider: Callable[[], str | None] = token_provider
        else:
            fixed = (token or "").strip() or None
            self._provider = lambda: fixed
        self.ttl_seconds = max(300, int(ttl_seconds))
        self._failures: dict[str, deque[float]] = {}

    # -- 配置态 -------------------------------------------------------------- #
    @property
    def _token(self) -> str | None:
        value = self._provider()
        value = (value or "").strip() or None
        return value

    @property
    def configured(self) -> bool:
        """没配令牌 = 面板不可登录（不是"免登录"，那样等于没有门）。"""
        return self._token is not None

    def _key(self, salt: bytes) -> bytes:
        current = self._token
        assert current is not None
        return hmac.new(current.encode("utf-8"), salt, hashlib.sha256).digest()

    def check_token(self, candidate: str) -> bool:
        current = self._token
        if current is None or not candidate:
            return False
        return hmac.compare_digest(candidate.strip(), current)

    # -- 会话 ---------------------------------------------------------------- #
    def _sign(self, issued_at: int) -> str:
        msg = f"{_SESSION_VERSION}.{issued_at}".encode()
        return hmac.new(self._key(_SALT_SESSION), msg, hashlib.sha256).hexdigest()

    def _csrf(self, issued_at: int) -> str:
        msg = f"{_SESSION_VERSION}.{issued_at}".encode()
        return hmac.new(self._key(_SALT_CSRF), msg, hashlib.sha256).hexdigest()[:40]

    def issue(self, *, now: float | None = None) -> tuple[str, Session]:
        """签发会话，返回 (cookie 值, 会话对象)。调用方负责写 Set-Cookie。"""
        if self.configured is False:
            raise RuntimeError("面板令牌未配置，无法签发会话")
        issued = int(now if now is not None else time.time())
        cookie = f"{_SESSION_VERSION}.{issued}.{self._sign(issued)}"
        return cookie, Session(issued_at=issued, csrf=self._csrf(issued))

    def verify(self, cookie: str | None, *, now: float | None = None) -> Session | None:
        """校验 Cookie；过期、被改、令牌换过都返回 None。"""
        if not cookie or self._token is None:
            return None
        parts = cookie.split(".")
        if len(parts) != 3 or parts[0] != _SESSION_VERSION:
            return None
        try:
            issued = int(parts[1])
        except ValueError:
            return None
        if not hmac.compare_digest(parts[2], self._sign(issued)):
            return None
        current = now if now is not None else time.time()
        if current - issued > self.ttl_seconds:
            return None
        if issued > current + 60:  # 明显是伪造的将来时间
            return None
        return Session(issued_at=issued, csrf=self._csrf(issued))

    # -- 登录限速 ------------------------------------------------------------ #
    def allow_attempt(self, ip: str, *, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        hits = self._failures.get(ip)
        if not hits:
            return True
        while hits and current - hits[0] > FAILURE_WINDOW_SECONDS:
            hits.popleft()
        return len(hits) < MAX_FAILURES

    def note_failure(self, ip: str, *, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        self._failures.setdefault(ip, deque(maxlen=MAX_FAILURES)).append(current)

    def note_success(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def reset(self) -> None:
        self._failures.clear()
