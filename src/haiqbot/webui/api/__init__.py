"""WebUI 的 API 路由表。

每个模块只导出 `routes(ctx)`，装配集中在这里 —— 加一个页面只需要
写一个模块 + 在 `MODULES` 里排一行，不用碰 server.py。
"""

from __future__ import annotations

from typing import Any

from haiqbot.webui.api import (
    channels,
    config_api,
    media,
    overview,
    panels,
    plugins,
    session,
    workbench,
)
from haiqbot.webui.api.common import Ctx

MODULES = (session, overview, plugins, config_api, channels, media, workbench, panels)


def build_routes(ctx: Ctx) -> list[Any]:
    out: list[Any] = []
    for module in MODULES:
        out.extend(module.routes(ctx))
    return out


__all__ = ["Ctx", "build_routes", "MODULES"]
