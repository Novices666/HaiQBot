"""插件自定义页（PanelPage）：插件自己往面板里长页面，内核只认这份声明式协议。

为什么声明式，而不是 iframe / 注入 JS：

- 面板能改配置、能触发动作，脚本注入等于把鉴权交给插件；
- 声明式 schema 让前端只渲染自己认识的 section，不认识的降级成只读 JSON ——
  插件写错了最多是页面难看，不会白屏，也不会执行陌生代码。

插件侧契约（鸭子类型，不用改基类）：

    def panel_meta(self) -> dict:                 # 可选，只影响导航
        return {"title": "我的页面", "order": 50}

    def panel_page(self, panel) -> dict:          # 也可以是 async
        return {"title": "…", "sections": [ … ]}

    async def panel_action(self, action: str, payload: dict) -> dict | str:
        return {"message": "已经做完了"}

`panel` 只给两样东西：插件自己的私有配置 + 只读运行时计数。**不给 vault、不给 store**
—— 面板不是后门，插件要写数据请走自己的能力层。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

from haiqbot.kernel.log import get_logger
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json

log = get_logger(__name__)

#: 前端认识的 section 类型；新增类型要同时改 web/src/v2/panels.tsx。
SECTION_TYPES = ("field_group", "readonly_table", "action_button", "text")


def _plugins(registry: Any) -> list[Any]:
    handles = getattr(registry, "handles", None)
    if not callable(handles):
        return []
    try:
        return [getattr(h, "plugin", None) for h in handles().values()]
    except Exception:  # noqa: BLE001
        return []


def _pid_of(plugin: Any) -> str | None:
    pid = getattr(plugin, "id", None)
    return str(pid) if pid else None


def panel_meta(plugin: Any) -> dict[str, Any] | None:
    """插件声明了 panel_page 才算有自定义页；meta 只影响导航顺序/标题。"""
    if not callable(getattr(plugin, "panel_page", None)):
        return None
    pid = _pid_of(plugin)
    if not pid:
        return None
    raw = getattr(plugin, "panel_meta", None)
    meta = raw() if callable(raw) else {}
    meta = dict(meta) if isinstance(meta, dict) else {}
    return {
        "pid": pid,
        "title": str(meta.get("title") or getattr(plugin, "name", "") or pid),
        "order": int(meta.get("order") or 100),
    }


def panel_index(registry: Any) -> list[dict[str, Any]]:
    """导航用的自定义页清单（不执行插件代码）。"""
    out = [meta for meta in (panel_meta(p) for p in _plugins(registry)) if meta]
    return sorted(out, key=lambda m: (m["order"], m["pid"]))


def allowed_actions(page: dict[str, Any]) -> set[str]:
    """页面上声明过的动作名。动作执行以此为准 —— 声明之外一律拒绝。"""
    names: set[str] = set()
    for section in page.get("sections") or []:
        if isinstance(section, dict) and section.get("type") == "action_button":
            action = str(section.get("action") or "").strip()
            if action:
                names.add(action)
    return names


async def _call(fn: Any, *args: Any) -> Any:
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


def _panel_ctx(runtime: Any, pid: str) -> Any:
    """给插件的面板上下文：只有自己的配置 + 只读计数，别的都不给。"""
    config = {}
    try:
        config = dict(getattr(runtime.config, "plugin_config", {}).get(pid) or {})
    except Exception:  # noqa: BLE001
        config = {}
    return SimpleNamespace(
        pid=pid,
        config=config,
        events_total=getattr(runtime, "events_total", 0),
        handled_total=getattr(runtime, "handled_total", 0),
    )


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime
    registry = ctx.registry

    def _find(pid: str) -> Any:
        for plugin in _plugins(registry):
            if _pid_of(plugin) == pid:
                return plugin
        return None

    async def _page_of(plugin: Any, pid: str) -> dict[str, Any]:
        page = await _call(plugin.panel_page, _panel_ctx(runtime, pid))
        if not isinstance(page, dict):
            raise TypeError("panel_page 必须返回 dict")
        out = dict(page)
        out["pid"] = pid
        out.setdefault("title", (panel_meta(plugin) or {}).get("title") or pid)
        sections = out.get("sections")
        out["sections"] = list(sections) if isinstance(sections, list) else []
        return out

    async def list_panels(_request: Any) -> Any:
        return ok(panel_index(registry))

    async def get_panel(request: Any) -> Any:
        pid = str(request.path_params["pid"])
        plugin = _find(pid)
        if plugin is None or panel_meta(plugin) is None:
            return fail(404, "not_found", f"插件 {pid} 没有自定义页")
        try:
            page = await _page_of(plugin, pid)
        except Exception as exc:  # noqa: BLE001 - 插件写坏了也不能把面板拖垮
            log.warning("panel_page_failed", plugin=pid, error=str(exc)[:200])
            return fail(500, "panel_error", f"{pid}.panel_page 抛错：{type(exc).__name__}: {exc}")
        page["actions"] = sorted(allowed_actions(page))
        return ok(page)

    async def run_action(request: Any) -> Any:
        pid = str(request.path_params["pid"])
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        action = str(body.get("action") or "").strip()
        if not action:
            return fail(400, "bad_request", "action 不能为空")
        plugin = _find(pid)
        if plugin is None:
            return fail(404, "not_found", f"没有插件 {pid}")
        handler = getattr(plugin, "panel_action", None)
        if not callable(handler):
            return fail(400, "no_action", f"{pid} 没有声明 panel_action")

        # 只允许页面上出现过的动作：避免把 panel_action 变成任意方法调用面
        try:
            page = await _page_of(plugin, pid)
        except Exception as exc:  # noqa: BLE001
            return fail(500, "panel_error", f"{pid}.panel_page 抛错：{exc}")
        if action not in allowed_actions(page):
            return fail(403, "action_not_declared", f"{action} 没有在页面里声明")

        try:
            result = await _call(handler, action, dict(body.get("payload") or {}))
        except Exception as exc:  # noqa: BLE001
            await audit(ctx, "panel.action", {"plugin": pid, "action": action, "ok": False})
            log.warning("panel_action_failed", plugin=pid, action=action, error=str(exc)[:200])
            return fail(500, "action_failed", f"{type(exc).__name__}: {exc}")

        await audit(ctx, "panel.action", {"plugin": pid, "action": action, "ok": True})
        if isinstance(result, dict):
            return ok({"action": action, **result})
        return ok({"action": action, "message": str(result)})

    return [
        # 放在 /api/channels/{channel} 之前：`verify` 不能被当成频道名
        Route("/api/panels", list_panels, methods=["GET"]),
        Route("/api/panels/{pid}", get_panel, methods=["GET"]),
        Route("/api/panels/{pid}/action", run_action, methods=["POST"]),
    ]


__all__ = ["SECTION_TYPES", "allowed_actions", "panel_index", "panel_meta", "routes"]
