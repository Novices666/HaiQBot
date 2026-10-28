"""插件与能力：列表、启停、重载、插件级配置。

写操作全部委托 `kernel/control.py`（CLI 与面板共用同一套语义），
这里只做 HTTP 包装 + 审计。面板不复制一份"自己的写逻辑"。
"""

from __future__ import annotations

from typing import Any

from haiqbot.kernel import control
from haiqbot.webui.api.common import Ctx, audit, fail, ok, read_json


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime
    registry = ctx.registry

    async def list_plugins(_request: Any) -> Any:
        rows = registry.status() if registry is not None else []
        commands = registry.commands() if registry is not None else []
        by_plugin: dict[str, list[dict[str, Any]]] = {}
        for cmd in commands:
            by_plugin.setdefault(cmd["plugin"], []).append(cmd)
        for row in rows:
            pid = row["id"]
            row["commands"] = by_plugin.get(pid, [])
            # 配置字段（面板直接按它渲染表单，不用二次请求）
            row["config_schema"] = registry.config_schema(pid) if registry is not None else []
            # 插件声明的凭据槽位（只有 label / 是否已设，**没有真值**）
            row["secrets"] = _secrets_view(runtime, row)
        return ok(rows)

    def _present(ref: str) -> bool:
        """凭据是否真的存在于 vault。

        注意：`registry._secrets_summary()` 只给 label/required/ref，**没有 set 字段**；
        以前这里读 `meta.get("set")` 永远是 None → 面板一律显示"未设置"，
        即使 vault 里明明有值。真正的存在性只能问 vault。
        """
        if not ref:
            return False
        vault = getattr(runtime, "vault", None)
        if vault is None:
            return False
        try:
            return vault.get(ref) is not None
        except Exception:  # noqa: BLE001 - vault 打不开按"未设置"显示，不能把面板搞挂
            return False

    def _secrets_view(runtime_obj: Any, row: dict[str, Any]) -> list[dict[str, Any]]:
        slots = row.get("secrets") or {}
        out: list[dict[str, Any]] = []
        if isinstance(slots, dict):
            for name, meta in slots.items():
                if not isinstance(meta, dict):
                    continue
                ref = str(meta.get("ref") or "")
                out.append(
                    {
                        "slot": name,
                        "label": meta.get("label") or name,
                        "ref": ref,
                        "required": bool(meta.get("required")),
                        "set": _present(ref),
                        "group": meta.get("group") or "",
                    }
                )
        return out

    async def detail(request: Any) -> Any:
        pid = request.path_params["pid"]
        rows = {row["id"]: row for row in (registry.status() if registry else [])}
        row = rows.get(pid)
        if row is None:
            return fail(404, "not_found", f"没有插件 {pid}")
        schema = registry.config_schema(pid) if registry is not None else None
        current = runtime.config.plugin_config.get(pid) or {}
        return ok({"plugin": row, "schema": schema, "config": current})

    async def set_config(request: Any) -> Any:
        pid = request.path_params["pid"]
        try:
            body = await read_json(request)
        except ValueError as exc:
            return fail(400, "bad_request", str(exc))
        try:
            result = control.apply_plugin_config(runtime, registry, pid, body)
        except control.ControlError as exc:
            return fail(exc.status_code, "control_error", exc.message)
        await audit(ctx, "plugin.config", {"plugin": pid, "changed": result})
        return ok(result)

    async def enable(request: Any) -> Any:
        return await _toggle(request, True)

    async def disable(request: Any) -> Any:
        return await _toggle(request, False)

    async def _toggle(request: Any, enabled: bool) -> Any:
        pid = request.path_params["pid"]
        try:
            result = await control.set_plugin_enabled(runtime, registry, pid, enabled)
        except control.ControlError as exc:
            return fail(exc.status_code, "control_error", exc.message)
        await audit(ctx, "plugin.enable" if enabled else "plugin.disable", result)
        return ok(result)

    async def reload(request: Any) -> Any:
        pid = request.path_params["pid"]
        try:
            result = await control.reload_plugin(registry, pid)
        except control.ControlError as exc:
            return fail(exc.status_code, "control_error", exc.message)
        except Exception as exc:  # noqa: BLE001 - 插件构造失败要让调用方看见
            await audit(ctx, "plugin.reload", {"id": pid, "error": str(exc)[:300]})
            return fail(500, "reload_failed", f"重载失败：{exc}")
        await audit(ctx, "plugin.reload", result)
        return ok(result)

    async def commands(_request: Any) -> Any:
        return ok(registry.commands() if registry is not None else [])

    def _declared_field(pid: str, key: str) -> dict[str, Any] | None:
        """从插件声明的 schema 里取某个字段的声明（不存在 = 不认识的字段）。"""
        for field in registry.config_schema(pid) if registry is not None else []:
            if str(field.get("key") or "") == key:
                return field
        return None

    async def field_options(request: Any) -> Any:
        """问插件要某个字段的可选值（模型列表这类**必须现问接口**的）。

        为什么不让插件在 `config_schema()` 里直接写死：那张表是静态声明，
        每次进面板都会算一遍；而这里要读 vault、要发 HTTP，只能按需触发。

        只读接口：不改任何状态，也不写审计（凭据真值永远不会出现在响应里，
        `SecretHandle` 自己会记 `secret_plain_read`）。
        """
        pid = request.path_params["pid"]
        key = request.path_params["key"]
        if registry is None:
            return fail(503, "no_registry", "插件系统未启动")
        decl = _declared_field(pid, key)
        if decl is None:
            return fail(404, "unknown_field", f"插件 {pid} 没有声明字段 {key}")
        if not decl.get("dynamic"):
            return fail(
                400,
                "not_dynamic",
                f"{key} 不是动态字段：它的可选值写在 config_schema 里",
            )
        # 只接受插件自己列出来的输入项，避免面板拿这个口子改别的参数
        overrides: dict[str, Any] = {}
        for name in decl.get("dynamic_inputs") or []:
            raw = request.query_params.get(str(name))
            if raw is None:
                continue
            overrides[str(name)] = raw.strip()[:500]
        result = await registry.config_options(pid, key, overrides)
        return ok(result)

    async def services(_request: Any) -> Any:
        """能力表：谁提供了什么服务、谁在消费。"""
        providers = {
            name: {
                "owner": runtime.provider_owner.get(name),
                "spec": _spec_view(runtime.service_specs.get(name)),
            }
            for name in runtime.providers
        }
        return ok(providers)

    def _spec_view(spec: Any) -> Any:
        if spec is None:
            return None
        if hasattr(spec, "to_dict"):
            return spec.to_dict()
        return {
            "name": getattr(spec, "name", None),
            "version": getattr(spec, "version", None),
            "methods": sorted(getattr(spec, "methods", {}) or {}),
        }

    return [
        Route("/api/plugins", list_plugins, methods=["GET"]),
        Route("/api/plugins/{pid}", detail, methods=["GET"]),
        # 动态可选值（模型列表这类要现问接口的）
        Route("/api/plugins/{pid}/options/{key}", field_options, methods=["GET"]),
        Route("/api/plugins/{pid}/config", set_config, methods=["POST"]),
        Route("/api/plugins/{pid}/enable", enable, methods=["POST"]),
        Route("/api/plugins/{pid}/disable", disable, methods=["POST"]),
        Route("/api/plugins/{pid}/reload", reload, methods=["POST"]),
        Route("/api/commands", commands, methods=["GET"]),
        Route("/api/services", services, methods=["GET"]),
    ]
