"""控制面：WebUI 写操作的认证、可编辑字段白名单、热更新与持久化。

三条硬约束（改这里之前先读）：

1. **白名单即边界**：只有 `EDITABLE` 里列出的字段能改。`*_ref`、`secrets_dir`、
   `db_path`、适配器连接参数一律不可写 —— 它们要么涉及凭据，要么改了会在
   运行期把进程搞成半新半旧的状态。
2. **密钥只报存在性**：任何返回值都不允许带出 vault 里的值，只给「已设置 / 未设置」。
3. **先落盘再热更**：持久化失败就不动内存，避免"面板显示已改、重启后打回原形"。
   例外是 `config_path` 缺失（比如全默认配置），此时照常热更但返回 warning。
"""

from __future__ import annotations

import hmac
import re
import uuid
from pathlib import Path
from typing import Any

from haiqbot.config import plugin_config_file
from haiqbot.kernel.config_schema import APPLY_LABELS, GROUP_LABELS, SCHEMA, Field, read_value, write_value
from haiqbot.kernel.config_schema import BY_PATH as _BY_PATH
from haiqbot.kernel.config_writer import write_config
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)


class ControlError(Exception):
    """控制面业务错误，`status_code` 直接映射 HTTP 状态码。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# 内核自己的凭据：键名来自配置，与任何插件无关（真值永不外带）。
# 插件声明的槽位**不在这里登记** —— 走 secret_index 动态遍历，见 _plugin_secret_status()。
CORE_SECRET_REFS: tuple[tuple[str, str, str], ...] = (
    ("onebot", "access_token_ref", "OneBot Access Token"),
    ("official", "app_secret_ref", "QQ 官方 AppSecret"),
    ("webui", "token_ref", "面板写操作令牌"),
)


def _secret_ref(runtime: Any, scope: str, key: str) -> str:
    """内核段的 `*_ref` 配置值。插件槽位不走这里。"""
    holder = getattr(runtime.config, scope, None)
    return str(getattr(holder, key, "") or "")


# -- 认证 ------------------------------------------------------------------ #


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def require_write_token(runtime: Any, authorization: str | None) -> None:
    """校验写操作令牌。不通过就抛 ControlError。"""
    cfg = runtime.config
    if not cfg.webui.write_enabled:
        raise ControlError("面板写操作已被关闭（webui.write_enabled = false）", 403)
    ref = cfg.webui.token_ref
    if not ref:
        raise ControlError("未配置 webui.token_ref，写操作已禁用", 403)
    expected = runtime.vault.get(ref) if runtime.vault is not None else None
    if not expected:
        raise ControlError(f"vault 中没有 {ref}，请先执行：haiq webui token --generate", 403)
    presented = _bearer(authorization)
    if not presented or not hmac.compare_digest(presented, expected):
        raise ControlError("写操作令牌无效", 401)


# -- 读取 ------------------------------------------------------------------ #


def _is_set(runtime: Any, ref: str) -> bool:
    """问 vault，不问配置 —— 配置里没有 `<slot>_ref` 不代表库里没有。"""
    if not ref or runtime.vault is None:
        return False
    try:
        return bool(runtime.vault.resolve(ref).present)
    except Exception:  # noqa: BLE001 - 后端故障不该让面板打不开
        return False


def _plugin_secret_status(runtime: Any) -> list[dict[str, Any]]:
    """插件槽位 → 状态。动态遍历插件类，内核不认识任何具体插件。"""
    from .plugin import resolve_secret_ref
    from .secret_index import slot_index

    out: list[dict[str, Any]] = []
    config = runtime.config
    for slot_key, entry in sorted(slot_index(config).items()):
        section = config.plugin_config.get(entry["plugin"]) or {}
        ref = resolve_secret_ref(entry["plugin"], entry["name"], entry["slot"], section)
        out.append(
            {
                "path": f"plugin:{slot_key}",
                "label": entry["slot"].label,
                "ref": ref,
                "set": _is_set(runtime, ref),
            }
        )
    return out


def _secret_status(runtime: Any) -> list[dict[str, Any]]:
    out = _plugin_secret_status(runtime)
    for scope, key, label in CORE_SECRET_REFS:
        ref = _secret_ref(runtime, scope, key)
        out.append({"path": f"{scope}.{key}", "label": label, "ref": ref, "set": _is_set(runtime, ref)})
    return out


def _jsonable(value: Any) -> Any:
    """把 Path / 元组之类转成 JSON 能表示的形式。"""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def describe(runtime: Any) -> dict[str, Any]:
    """面板配置页的数据源。包含字段当前值与密钥存在性，**不含任何密钥真值**。"""
    cfg = runtime.config
    fields: list[dict[str, Any]] = []
    for spec in SCHEMA:
        fields.append(
            {
                "path": spec.path,
                "section": spec.section,
                "key": spec.key,
                "kind": spec.kind,
                "label": spec.label,
                "group": spec.group,
                "group_label": GROUP_LABELS.get(spec.group, spec.group),
                "apply": spec.apply,
                "apply_label": APPLY_LABELS.get(spec.apply, spec.apply),
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "choices": list(spec.choices),
                "readonly": spec.readonly,
                "help": spec.help,
                "value": _jsonable(read_value(cfg, spec)),
            }
        )
    return {
        "write_enabled": cfg.webui.write_enabled,
        "config_path": str(cfg.config_path) if cfg.config_path else None,
        "fields": fields,
        "secrets": _secret_status(runtime),
    }


# -- 写入 ------------------------------------------------------------------ #


def _coerce(spec: Field, raw: Any) -> Any:
    if spec.readonly:
        raise ControlError(f"{spec.label} 是只读字段（{APPLY_LABELS.get(spec.apply, spec.apply)}）", 403)
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.lower() in {"true", "false", "1", "0"}:
            return raw.lower() in {"true", "1"}
        raise ControlError(f"{spec.label} 需要布尔值")
    if spec.kind in {"int", "float"}:
        if isinstance(raw, bool):
            raise ControlError(f"{spec.label} 需要数字")
        try:
            number = int(raw) if spec.kind == "int" else float(raw)
        except (TypeError, ValueError) as exc:
            raise ControlError(f"{spec.label} 需要数字") from exc
        if spec.minimum is not None and number < spec.minimum:
            raise ControlError(f"{spec.label} 不能小于 {spec.minimum:g}")
        if spec.maximum is not None and number > spec.maximum:
            raise ControlError(f"{spec.label} 不能大于 {spec.maximum:g}")
        return number
    if spec.kind == "list":
        if isinstance(raw, str):
            items = [x.strip() for x in re.split(r"[,\n]", raw) if x.strip()]
        elif isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw if str(x).strip()]
        else:
            raise ControlError(f"{spec.label} 需要列表")
        if len(items) > 500:
            raise ControlError(f"{spec.label} 条目过多（上限 500）")
        return items
    # str / enum / path / ref
    if not isinstance(raw, str):
        raise ControlError(f"{spec.label} 需要字符串")
    text = raw.strip()
    if spec.kind == "enum" and spec.choices and text not in spec.choices:
        raise ControlError(f"{spec.label} 只能是：{' / '.join(spec.choices)}")
    if len(text) > 4000:
        raise ControlError(f"{spec.label} 过长（上限 4000 字符）")
    return text


def _sync_derived(runtime: Any, path: str) -> None:
    """把配置变更同步到「构造时拷走了值」的运行时对象上。

    其余对象持的是同一份 pydantic 子模型引用，`setattr` 自动生效。
    """
    cfg = runtime.config
    if path == "policy.handle_timeout_seconds" and runtime.bus is not None:
        runtime.bus.timeout = cfg.policy.handle_timeout_seconds
    elif path == "policy.max_handlers_per_event" and runtime.bus is not None:
        runtime.bus.max_handlers = cfg.policy.max_handlers_per_event
    elif path in {"policy.owners", "policy.admins"}:
        # RoleResolver 构造时把名单拷成了 set，必须重新灌
        runtime.resolver.owners = set(cfg.policy.owners)
        runtime.resolver.admins = set(cfg.policy.admins)
    elif path in {"log.level", "log.json"}:
        from haiqbot.kernel.log import configure_logging

        configure_logging(cfg.log_level, cfg.log_json)


def apply_changes(runtime: Any, changes: dict[str, Any]) -> dict[str, Any]:
    """校验 → 落盘 → 热更。返回 `{"changed": [...], "warnings": [...]}`。"""
    if not isinstance(changes, dict) or not changes:
        raise ControlError("没有要修改的字段")
    unknown = [p for p in changes if p not in _BY_PATH]
    if unknown:
        raise ControlError("以下字段不允许通过面板修改：" + ", ".join(sorted(unknown)), 403)
    cfg = runtime.config
    parsed: dict[str, Any] = {}
    for path, raw in changes.items():
        spec = _BY_PATH[path]
        current = read_value(cfg, spec)
        value = _coerce(spec, raw)
        if value != current:
            parsed[path] = value
    if not parsed:
        return {"changed": [], "warnings": ["全部值未变化，无需写入"]}
    warnings: list[str] = []
    # 先落盘：写盘失败就整体放弃，不留下「内存改了、文件没改」的状态
    if cfg.config_path:
        try:
            write_config(
                Path(cfg.config_path),
                {_BY_PATH[p].toml_path: v for p, v in parsed.items()},
            )
        except Exception as exc:  # noqa: BLE001
            raise ControlError(f"配置写入失败，已放弃本次修改：{exc}", 500) from exc
    else:
        warnings.append("未定位到配置文件，改动仅在内存生效（重启即丢失）")
    changed: list[dict[str, Any]] = []
    for path, value in parsed.items():
        spec = _BY_PATH[path]
        old = read_value(cfg, spec)
        write_value(cfg, spec, value)
        _sync_derived(runtime, path)
        changed.append(
            {
                "path": path,
                "old": old,
                "new": value,
                "apply": spec.apply,
                "apply_label": APPLY_LABELS.get(spec.apply, spec.apply),
            }
        )
    deferred = sorted({c["apply_label"] for c in changed if c["apply"] != "hot"})
    if deferred:
        warnings.append("以下改动需要额外操作才生效：" + "、".join(deferred))
    return {"changed": changed, "warnings": warnings}


def _persist_enabled(cfg: Any, ids: list[str]) -> None:
    if not cfg.config_path:
        return
    write_config(Path(cfg.config_path), {"plugins.enabled": list(ids)})


async def set_plugin_enabled(runtime: Any, registry: Any, pid: str, enabled: bool) -> dict[str, Any]:
    """启停插件：同步 `plugins.enabled` + 运行时装载/卸载 + 落盘。"""
    known = registry.discover()
    if pid not in known:
        raise ControlError(f"找不到插件 {pid}", 404)
    enabled_ids: list[str] = runtime.config.plugins_enabled
    if enabled:
        if pid in enabled_ids:
            handle = registry.handles().get(pid)
            if handle is not None:
                return {"id": pid, "enabled": True, "loaded": True, "note": "已在运行"}
        try:
            _persist_enabled(runtime.config, [*enabled_ids, pid] if pid not in enabled_ids else enabled_ids)
        except Exception as exc:  # noqa: BLE001
            raise ControlError(f"配置写入失败，已放弃启用：{exc}", 500) from exc
        if pid not in enabled_ids:
            enabled_ids.append(pid)
        await registry.load(pid)
        return {"id": pid, "enabled": True, "loaded": True, "note": "已启用并加载"}
    try:
        _persist_enabled(runtime.config, [x for x in enabled_ids if x != pid])
    except Exception as exc:  # noqa: BLE001
        raise ControlError(f"配置写入失败，已放弃停用：{exc}", 500) from exc
    while pid in enabled_ids:
        enabled_ids.remove(pid)
    unloaded = await registry.unload(pid)
    return {"id": pid, "enabled": False, "loaded": False, "note": "已停用" if unloaded else "原本未加载"}


async def reload_plugin(registry: Any, pid: str) -> dict[str, Any]:
    known = registry.discover()
    if pid not in known:
        raise ControlError(f"找不到插件 {pid}", 404)
    if pid not in registry.handles():
        raise ControlError(f"插件 {pid} 未启用，无法重载（请先启用）", 409)
    await registry.reload(pid)
    return {"id": pid, "reloaded": True}


def new_trace_id() -> str:
    return uuid.uuid4().hex[:12]


# -- 插件私有参数（haiq.toml 的 [plugins.<id>]） ---------------------------- #

_PLUGIN_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
# *_ref 的**值**只能是键名，不能是密钥本身
_REF_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _coerce_plugin_value(key: str, raw: Any) -> Any:
    if not _PLUGIN_KEY_RE.match(key):
        raise ControlError(f"参数名不合法：{key!r}（只允许字母、数字、下划线）")
    if isinstance(raw, bool) or isinstance(raw, (int, float)):
        return raw
    if raw is None:
        raise ControlError(f"{key} 的值不能为 null")
    if not isinstance(raw, str):
        raise ControlError(f"{key} 只支持字符串 / 数字 / 布尔")
    if len(raw) > 4000:
        raise ControlError(f"{key} 过长（上限 4000 字符）")
    if key.endswith("_ref") and not _REF_VALUE_RE.match(raw):
        raise ControlError(f"{key} 只能填 vault 里的键名（字母/数字/._-），不要粘贴密钥本身")
    return raw


def plugin_settings(runtime: Any, registry: Any, pid: str) -> dict[str, Any]:
    """单个插件的设置页数据：当前值 + 它自己声明的表单。"""
    if pid not in registry.discover():
        raise ControlError(f"找不到插件 {pid}", 404)
    current = dict(runtime.config.plugin_config.get(pid, {}))
    schema = registry.config_schema(pid)
    # 声明里有、配置里没有的键，用 default 兜底显示
    for field in schema:
        key = field.get("key")
        if key and key not in current and "default" in field:
            current[key] = field["default"]
    return {
        "id": pid,
        "config": current,
        "schema": schema,
        "loaded": pid in registry.handles(),
    }


def apply_plugin_config(runtime: Any, registry: Any, pid: str, changes: dict[str, Any]) -> dict[str, Any]:
    """写 `[plugins.<id>]` 并热更新内存里的 `ctx.config`。

    注意：`ctx.config` 是 registry 加载时对配置做的一份拷贝，
    所以必须两边都改 —— 只落盘的话运行中的插件看不到新值。
    """
    if pid not in registry.discover():
        raise ControlError(f"找不到插件 {pid}", 404)
    if not isinstance(changes, dict) or not changes:
        raise ControlError("没有要修改的参数")
    parsed = {k: _coerce_plugin_value(k, v) for k, v in changes.items()}
    persisted = runtime.config.plugin_config.get(pid)
    if persisted is None:
        persisted = {}
        runtime.config.plugin_config[pid] = persisted
    # 先落盘（失败就整体放弃）。落点是插件自己的文件，主 toml 只留注册。
    try:
        target = plugin_config_file(runtime.config.data_dir, pid)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_config(target, dict(parsed))
    except Exception as exc:  # noqa: BLE001
        raise ControlError(f"配置写入失败，已放弃本次修改：{exc}", 500) from exc
    changed: list[dict[str, Any]] = []
    handle = registry.handles().get(pid)
    for key, value in parsed.items():
        old = persisted.get(key)
        # ctx.config 是 LiveConfig，伸向同一个 dict —— 写这一处就够了
        persisted[key] = value
        if old != value:
            changed.append({"key": key, "old": old, "new": value})
    note = "已生效" if handle is not None else "插件未加载，启用后生效"
    if handle is not None and changed:
        note = "已热更新；若该插件在 on_load 里读取参数，请点『重载』"
    return {"id": pid, "changed": changed, "applied_live": handle is not None, "note": note}
