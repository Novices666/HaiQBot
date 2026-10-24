"""面板服务器：静态资源 + SPA 回退 + 鉴权中间件。

一个门只开一次：
- `/api/auth/login` 免鉴权；
- 其余 `/api/*` 必须带有效会话 Cookie；
- 非 GET 还必须带 `X-Haiq-CSRF`（值在登录响应 / `/api/auth/session` 里给）；
- `webui.write_enabled=false` 时所有非 GET 一律 403（面板退回纯仪表盘）。

静态资源约定：`dist/` 由 Vite 构建（产物入库），SPA 路由由后端兜底到 index.html，
所以刷新 `/plugins` 这种前端路由不会 404。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from haiqbot.kernel.log import get_logger
from haiqbot.webui.api import build_routes
from haiqbot.webui.api.common import Ctx
from haiqbot.webui.auth import COOKIE_NAME, Auth

log = get_logger(__name__)

# Windows 的注册表里常常没有 .woff2/.woff 的 MIME 映射，StaticFiles 会退回
# application/octet-stream；浏览器（尤其 Firefox）会因此拒绝加载自托管字体。
# 这里显式登记，保证跨平台行为一致。
for _ext, _mime in (
    (".woff2", "font/woff2"),
    (".woff", "font/woff"),
    (".ttf", "font/ttf"),
    (".otf", "font/otf"),
):
    mimetypes.add_type(_mime, _ext)

_DIST = Path(__file__).parent / "dist"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

_MISSING_DIST = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>haiqbot 面板未构建</title>
<style>body{font:15px/1.7 system-ui,sans-serif;max-width:44rem;margin:14vh auto;padding:0 1.5rem;
color:#e6e8eb;background:#0e1116}code{background:#1a2027;padding:.15em .45em;border-radius:4px}
h1{font-size:1.35rem;margin-bottom:.6rem}p{color:#9aa4af}</style>
<h1>面板前端还没构建</h1>
<p>后端已经起来了，但 <code>src/haiqbot/webui/dist/</code> 不存在。</p>
<p>构建一次即可：<code>cd web &amp;&amp; npm ci &amp;&amp; npm run build</code></p>
<p>构建产物会写进 <code>src/haiqbot/webui/dist/</code>，随包分发，部署机不需要 node。</p>
</html>"""


def _session_of(request: Any, auth: Auth) -> Any:
    return auth.verify(request.cookies.get(COOKIE_NAME))


def build_webui(runtime: Any, registry: Any, *, config_path: Path | str | None = None) -> Starlette:
    """装配面板。`config_path` 缺省用 runtime.config.config_path。"""
    cfg = runtime.config

    def _token() -> str | None:
        """每次现取：改令牌不用重启（拿不到就当没配）。"""
        try:
            return runtime.vault.get(cfg.webui.token_ref)
        except Exception:  # noqa: BLE001 - vault 不可用时面板仍然起得来，只是不能登录
            log.warning("webui_token_unavailable", ref=cfg.webui.token_ref)
            return None

    auth = Auth(token_provider=_token)
    if not auth.configured:
        log.warning(
            "webui_token_missing",
            ref=cfg.webui.token_ref,
            hint="运行 `haiq webui token --generate`，否则面板无法登录",
        )

    resolved_config = Path(config_path or getattr(cfg, "config_path", None) or "haiq.toml")
    ctx = Ctx(
        runtime=runtime,
        registry=registry,
        auth=auth,
        config_path=resolved_config,
        write_enabled=bool(cfg.webui.write_enabled),
    )

    async def index(_request: Any) -> Response:
        target = _DIST / "index.html"
        if target.is_file():
            return FileResponse(target, media_type="text/html")
        return HTMLResponse(_MISSING_DIST)

    async def spa(request: Any) -> Response:
        """非 /api、非 /i 的路径都交给前端路由。"""
        rel = request.path_params.get("path") or ""
        if rel.startswith(("api/", "i/")):
            return JSONResponse(
                {"ok": False, "error": {"code": "not_found", "message": "没有这个接口"}},
                status_code=404,
            )
        candidate = (_DIST / rel).resolve()
        # 防目录穿越：只允许 dist 目录内的文件
        if _DIST.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return await index(request)

    app = Starlette(
        routes=[
            *build_routes(ctx),
            Route("/", index, methods=["GET"]),
            Mount("/assets", app=StaticFiles(directory=_DIST / "assets", check_dir=False), name="assets"),
            Route("/{path:path}", spa, methods=["GET"]),
        ]
    )

    class Guard(BaseHTTPMiddleware):
        """一道门：会话 → 只读开关 → CSRF，顺序不能反。"""

        async def dispatch(self, request: Any, call_next: Any) -> Response:
            path = request.url.path
            method = request.method.upper()
            session = _session_of(request, auth)
            request.state.session = session

            if path.startswith("/api/") and path != "/api/auth/login":
                if session is None:
                    return JSONResponse(
                        {
                            "ok": False,
                            "error": {
                                "code": "unauthorized" if auth.configured else "token_missing",
                                "message": "未登录或会话已过期"
                                if auth.configured
                                else "面板令牌未配置，请先运行 `haiq webui token --generate`",
                            },
                        },
                        status_code=401,
                    )
                if method not in _SAFE_METHODS:
                    if not ctx.write_enabled:
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": {
                                    "code": "readonly",
                                    "message": "面板是只读模式（webui.write_enabled=false）",
                                },
                            },
                            status_code=403,
                        )
                    csrf = request.headers.get("x-haiq-csrf") or ""
                    if not csrf or csrf != session.csrf:
                        return JSONResponse(
                            {
                                "ok": False,
                                "error": {
                                    "code": "csrf",
                                    "message": "缺少或错误的 X-Haiq-CSRF 头",
                                },
                            },
                            status_code=403,
                        )
            return await call_next(request)

    app.add_middleware(Guard)
    return app


__all__ = ["build_webui", "Auth", "COOKIE_NAME"]
