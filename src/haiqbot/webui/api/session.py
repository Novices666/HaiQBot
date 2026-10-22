"""登录 / 登出 / 会话信息。

只有 `/api/auth/login` 是免鉴权的（否则没法进门）；
其余接口全部由 `server.py` 的中间件校验会话 Cookie。
"""

from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from haiqbot.webui.api.common import Ctx, fail, ok, read_json
from haiqbot.webui.auth import COOKIE_NAME


def _client_ip(request: Any) -> str:
    # 反代场景优先 X-Forwarded-For 的第一段；只有本机监听时 host 就是 127.0.0.1
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


def _set_cookie(response: JSONResponse, value: str, *, max_age: int, secure: bool) -> None:
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    auth = ctx.auth

    async def login(request: Any) -> JSONResponse:
        if not auth.configured:
            return fail(
                503,
                "token_missing",
                "面板令牌未配置，无法登录",
                hint="运行 `haiq webui token --generate` 生成令牌后重启 haiqbot",
            )
        ip = _client_ip(request)
        if not auth.allow_attempt(ip):
            return fail(
                429,
                "too_many_attempts",
                "登录失败次数过多，请稍后再试",
                hint="60 秒内最多 5 次",
            )
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        if not auth.check_token(str(body.get("token") or "")):
            auth.note_failure(ip)
            return fail(401, "bad_token", "令牌不正确")
        auth.note_success(ip)
        cookie, session = auth.issue()
        # 反代 + HTTPS 时才加 Secure；本机 http://127.0.0.1 上加 Secure 会让浏览器直接丢弃 Cookie
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response = ok(
            {
                "expires_at": session.issued_at + auth.ttl_seconds,
                "csrf": session.csrf,
                "write_enabled": ctx.write_enabled,
            }
        )
        _set_cookie(response, cookie, max_age=auth.ttl_seconds, secure=secure)
        return response

    async def logout(_request: Any) -> JSONResponse:
        response = ok({"logged_out": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    async def session_info(request: Any) -> JSONResponse:
        session = getattr(request.state, "session", None)
        return ok(
            {
                "csrf": session.csrf if session else None,
                "expires_at": (session.issued_at + auth.ttl_seconds) if session else None,
                "write_enabled": ctx.write_enabled,
                "token_configured": auth.configured,
                "ttl_seconds": auth.ttl_seconds,
            }
        )

    return [
        Route("/api/auth/login", login, methods=["POST"]),
        Route("/api/auth/logout", logout, methods=["POST"]),
        Route("/api/auth/session", session_info, methods=["GET"]),
    ]
