"""Web 仪表盘（Vite + React/TS 前端 + Starlette 后端）。

模块划分：
- `server.py`  装配 Starlette、静态资源、SPA 回退、鉴权中间件
- `auth.py`    单令牌 + 无状态会话 Cookie + CSRF
- `api/`       每个领域一个模块，统一 `routes(ctx)` 出口
- `dist/`      前端构建产物（入库，随 wheel 分发）

这里只导出 `build_webui` —— 外部（`haiqbot/app.py`）只认这一个入口。
"""

from haiqbot.webui.server import build_webui

__all__ = ["build_webui"]
