"""插件注册器：发现 → 校验 → 加载 → 注册到总线 → 可卸载/重载。

三种来源：
1. pip 包的 entry point（`haiqbot.plugins` 组）—— 第三方插件
2. 本地目录（含 `plugin.toml`）—— 开发中的插件
3. 随仓库分发的插件（放在 plugin_dirs 里，与第三方插件同一条协议）
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from haiqbot.core.channel import ChannelKind, Role
from haiqbot.core.errors import ContractError, HaiqError
from haiqbot.core.event import EventKind
from haiqbot.kernel import plugin_sources
from haiqbot.kernel.capabilities import IntrospectCap, build_capabilities
from haiqbot.kernel.log import get_logger
from haiqbot.kernel.matcher import MatchRule
from haiqbot.kernel.plugin import (
    PLUGIN_API_VERSION,
    Context,
    LiveConfig,
    Plugin,
    PluginManifest,
    ProviderContext,
    SecretHandle,
    SecretSlot,
    ServiceProxy,
    ServiceSpec,
    parse_version_requirement,
    resolve_secret_ref,
)
from haiqbot.kernel.runtime import Runtime

log = get_logger(__name__)

ENTRY_GROUP = plugin_sources.ENTRY_GROUP


def _provides_summary(provides: Any) -> dict[str, Any]:
    """把 provides 统一成面板能直接渲染的形状。"""
    out: dict[str, Any] = {}
    for name, declared in (provides or {}).items():
        if isinstance(declared, ServiceSpec):
            out[name] = declared.to_dict()
        else:
            out[name] = {"factory": str(declared), "version": None}
    return out


def _secrets_summary(plugin_obj: Any, cfg: Mapping[str, Any]) -> dict[str, Any]:
    """面板/doctor 用的槽位摘要。只给标签与键名，绝不含值。"""
    pid = str(getattr(plugin_obj, "id", "") or "")
    out: dict[str, Any] = {}
    for name, slot in (getattr(plugin_obj, "secrets", None) or {}).items():
        if not isinstance(slot, SecretSlot):
            continue
        out[name] = {
            "label": slot.label,
            "required": slot.required,
            "ref": resolve_secret_ref(pid, name, slot, cfg),
            "description": slot.description,
        }
    return out


def _wants_provider_context(factory: Any) -> bool:
    """工厂方法要不要 ProviderContext？

    零参数的简单提供者（`def build(self)`）很常见，不该被强制改签名。
    """
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建/装饰过的可调用对象
        return False
    return len(params) >= 1


@dataclass
class PluginHandle:
    plugin: Plugin
    manifest: PluginManifest
    context: Context
    rules: list[MatchRule]
    module_name: str | None = None


class Registry:
    def __init__(
        self,
        runtime: Runtime,
        *,
        storage_root: Path | None = None,
        net_hosts: list[str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.storage_root = storage_root
        self.net_hosts = net_hosts
        self._handles: dict[str, PluginHandle] = {}
        self._errors: dict[str, str] = {}
        # 依赖不满足而「暂停」的插件：pid -> 缺失说明列表
        self._blocked: dict[str, list[str]] = {}
        # 插件声明的可配置项缓存（控制台「插件设置」页用）
        self._schemas: dict[str, list[dict[str, Any]]] = {}
        # 未加载的插件也要能显示版本/描述：从 entry point 的类属性读，不实例化
        self._class_cache: dict[str, type] | None = None

    def _slot_handles(self, pid: str, cfg: Mapping[str, Any], plugin: Any) -> dict[str, SecretHandle]:
        """按插件声明的 `secrets` 发放句柄。没声明就是空的。"""
        declared = getattr(plugin, "secrets", None) or {}
        out: dict[str, SecretHandle] = {}
        for name, slot in declared.items():
            if not isinstance(slot, SecretSlot):
                log.warning("secret_slot_invalid", plugin=pid, slot=name)
                continue
            out[name] = SecretHandle(pid, name, slot, cfg, self.runtime.vault, audit=self.runtime.audit)
        return out

    # -- 发现 ------------------------------------------------------------- #
    def discover(self) -> dict[str, str]:
        """返回 {plugin_id: 来源描述}，不加载。"""
        found: dict[str, str] = {}
        for name, value in self._entry_points().items():
            found[name] = f"entrypoint:{value}"
        for pid, path in self._local_dirs().items():
            found[pid] = f"local:{path}"
        return found

    def _entry_points(self) -> dict[str, str]:
        return plugin_sources.entry_point_map()

    def _local_dirs(self) -> dict[str, Path]:
        return plugin_sources.local_plugin_dirs(self.runtime.config)

    # -- 导入 ------------------------------------------------------------- #
    def _import_target(self, pid: str) -> tuple[Any, str, str | None]:
        """返回 (module, source, module_name)。

        entry point 优先，但它可能指向已经被搬走/卸载的模块（例如装过旧版
        wheel 留下的 dist-info）。这种入口视为失效，**回落本地目录**而不是
        直接失败 —— 否则一次安装残留就让插件永久起不来。
        """
        eps = self._entry_points()
        if pid in eps:
            mod_name, _, attr = eps[pid].partition(":")
            try:
                module = importlib.import_module(mod_name)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "plugin_entrypoint_stale",
                    plugin=pid,
                    target=eps[pid],
                    error=f"{type(exc).__name__}: {exc}",
                    hint="entry point 指向的模块导不进来，改用本地目录",
                )
            else:
                target = getattr(module, attr, None) if attr else module
                if target is not None:
                    return target, "entrypoint", mod_name

        locals_ = self._local_dirs()
        if pid in locals_:
            path = locals_[pid]
            try:
                module = plugin_sources.load_local_module(pid, path)
            except Exception as exc:  # noqa: BLE001
                raise HaiqError(f"本地插件 {pid} 加载失败：{path}（{type(exc).__name__}: {exc}）") from exc
            return module, "local", f"haiqbot_local_{pid}"

        raise HaiqError(
            f"找不到插件 {pid}：既不是已安装的 entry point（haiqbot.plugins 组），"
            f"也不在 plugin_dirs 里。检查 haiq.toml 的 plugin_dirs 与 [plugins] enabled。"
        )

    def _pick_plugin(self, target: Any, pid: str) -> type[Plugin]:
        if inspect.isclass(target) and issubclass(target, Plugin) and target is not Plugin:
            return target
        candidates = [
            obj
            for _, obj in inspect.getmembers(target, inspect.isclass)
            if issubclass(obj, Plugin)
            and obj is not Plugin
            and obj.__module__.startswith(getattr(target, "__name__", ""))
        ]
        if not candidates:
            candidates = [
                obj
                for _, obj in inspect.getmembers(target, inspect.isclass)
                if issubclass(obj, Plugin) and obj is not Plugin
            ]
        for cls in candidates:
            if getattr(cls, "id", None) == pid:
                return cls
        if candidates:
            return candidates[0]
        raise HaiqError(f"插件 {pid} 中没有找到 Plugin 子类")

    # -- 清单覆盖 --------------------------------------------------------- #
    def _toml_meta(self, pid: str) -> dict[str, Any]:
        path = self._local_dirs().get(pid)
        if path is None:
            return {}
        meta_file = path / "plugin.toml"
        try:
            with meta_file.open("rb") as fh:
                return tomllib.load(fh)
        except Exception:  # noqa: BLE001
            return {}

    def _apply_toml_meta(self, pid: str, manifest: PluginManifest) -> PluginManifest:
        """本地插件的 plugin.toml 优先于类属性，方便不改代码调整清单。"""
        meta = self._toml_meta(pid)
        if not meta:
            return manifest
        patch: dict[str, Any] = {}
        for key in ("name", "version", "description", "isolation"):
            if key in meta:
                patch[key] = str(meta[key])
        if "capabilities" in meta:
            patch["capabilities"] = tuple(meta["capabilities"])
        if "requires" in meta:
            patch["requires"] = tuple(meta["requires"])
        if "api_version" in meta:
            patch["api_version"] = int(meta["api_version"])
        return dataclasses.replace(manifest, **patch) if patch else manifest

    # -- 生命周期 --------------------------------------------------------- #
    async def load(self, pid: str) -> PluginHandle:
        if pid in self._handles:
            return self._handles[pid]

        target, source, mod_name = self._import_target(pid)
        cls = self._pick_plugin(target, pid)
        plugin = cls()
        manifest: PluginManifest = plugin.manifest(source)
        if source == "local":
            manifest = self._apply_toml_meta(pid, manifest)

        if manifest.api_version != PLUGIN_API_VERSION:
            raise HaiqError(
                f"插件 {pid} 的 api_version={manifest.api_version} "
                f"与内核 {PLUGIN_API_VERSION} 不兼容，已拒绝加载"
            )
        if manifest.isolation != "inline":
            log.warning(
                "isolation_fallback",
                plugin=pid,
                requested=manifest.isolation,
                applied="inline",
                hint="子进程隔离在 M8 实现，当前按 inline 加载",
            )

        # 不拷贝：ctx.config 是伸进 runtime.config.plugin_config 的活视图
        cfg = LiveConfig(self.runtime.config.plugin_config, pid)
        caps = build_capabilities(
            manifest,
            get_service=self.runtime.get_service,
            store=self.runtime.store,
            scheduler=self.runtime.scheduler,
            storage_root=self.storage_root,
            net_hosts=self.net_hosts,
            quota=self.runtime.quota,
            adapters=self.runtime.adapters,
            audit=self.runtime.audit,
        )
        caps["introspect"] = IntrospectCap(self)
        # 服务级依赖是硬约束：声明了就必须已经有人提供，且版本够、方法齐。
        # 这里在 on_load 之前核对，插件就不用自己写"服务有没有"的防御代码。
        declared_services = dict(getattr(plugin, "services", None) or {})
        # 插件可以用自己提供的服务（比如 painter 既是 provider 又是 consumer）
        self_provides = set((getattr(plugin, "provides", None) or {}).keys())
        self._check_declared_services(pid, declared_services, self_provides=self_provides)
        ctx = Context(
            trace_id="",
            plugin_id=pid,
            log=log.bind(plugin=pid),
            config=cfg,
            services={name: ServiceProxy(name, self.runtime.get_service) for name in declared_services},
            **caps,
        )
        plugin.ctx = ctx
        plugin.config = cfg

        await plugin.on_load(ctx)

        # 提供能力：插件把自己实现的服务注册进内核，消费方惰性解析
        for cap_name, declared in (getattr(plugin, "provides", None) or {}).items():
            spec, method_name = self._normalise_provide(cap_name, declared)
            factory = getattr(plugin, method_name, None)
            if factory is None:
                log.warning(
                    "provider_method_missing",
                    plugin=pid,
                    capability=cap_name,
                    method=method_name,
                )
                continue
            if _wants_provider_context(factory):
                provider_ctx = ProviderContext(
                    plugin_id=pid,
                    config=cfg,
                    slots=self._slot_handles(pid, cfg, plugin),
                    media=self.runtime.media,
                    log=ctx.log,
                )
                service = factory(provider_ctx)
            else:
                service = factory()
            if inspect.isawaitable(service):
                service = await service
            # 契约校验：声明了 method 就必须真的有，否则**拒绝提供**
            # （宁可在加载时炸，也不要让消费方在运行时 AttributeError）
            if spec is not None and spec.methods:
                missing = [m for m in spec.methods if not hasattr(service, m)]
                if missing:
                    raise ContractError(f"插件 {pid} 提供的服务 {cap_name} 缺少契约方法：{missing}")
            self.runtime.provide(cap_name, service, pid, spec=spec)
            log.info(
                "capability_provided",
                plugin=pid,
                capability=cap_name,
                version=getattr(spec, "version", None),
            )

        rules: list[MatchRule] = []
        for func, spec in plugin.iter_matchers():
            rule = self._build_rule(pid, func, spec)
            rule.invoker = self._make_invoker(rule, plugin, ctx)
            self.runtime.bus.register(rule)
            rules.append(rule)

        handle = PluginHandle(
            plugin=plugin, manifest=manifest, context=ctx, rules=rules, module_name=mod_name
        )
        self._handles[pid] = handle
        self._errors.pop(pid, None)
        log.info(
            "plugin_loaded",
            plugin=pid,
            version=manifest.version,
            source=source,
            rules=len(rules),
            capabilities=list(manifest.capabilities),
        )
        return handle

    async def unload(self, pid: str) -> bool:
        handle = self._handles.pop(pid, None)
        if handle is None:
            return False
        self.runtime.bus.unregister_plugin(pid)
        withdrawn = self.runtime.withdraw(pid)
        if withdrawn:
            log.info("capabilities_withdrawn", plugin=pid, capabilities=withdrawn)
        try:
            await handle.plugin.on_unload()
        except Exception:  # noqa: BLE001
            log.exception("plugin_unload_error", plugin=pid)
        cap_ctx = handle.context
        for cap_name in ("net", "schedule"):
            cap = getattr(cap_ctx, cap_name, None)
            if cap is None:
                continue
            try:
                if cap_name == "net":
                    await cap.aclose()
                else:
                    cap.cancel_all()
            except Exception:  # noqa: BLE001
                pass
        if handle.module_name:
            for name in list(sys.modules):
                if name == handle.module_name or name.startswith(handle.module_name + "."):
                    sys.modules.pop(name, None)
        log.info("plugin_unloaded", plugin=pid)
        return True

    async def reload(self, pid: str) -> PluginHandle:
        await self.unload(pid)
        return await self.load(pid)

    # -- 批量加载 --------------------------------------------------------- #
    async def load_enabled(self) -> list[str]:
        enabled = list(self.runtime.config.plugins_enabled)
        try:
            ordered = self._topo_sort(enabled)
        except ContractError as exc:
            # 依赖成环：保持声明的顺序继续启动，并把这个错误显示在面板上。
            # 因为环境里有互相依赖的插件时，正确做法是提示用户改配置，
            # 而不是让整个 bot 起不来 —— 单个插件的服务依赖会在 load 时再被拦。
            self._errors["__dependencies__"] = str(exc)
            log.error("plugin_dependency_cycle", detail=str(exc))
            ordered = enabled
        loaded: list[str] = []
        pending = list(ordered)
        # 两阶段：先按顺序尝试加载，缺依赖的进 blocked；每成功一个就重试一轮 blocked。
        # 这样"提供方写在启用清单后面"也不会误判成失败。
        while pending:
            progressed = False
            for pid in list(pending):
                missing = self._service_problems(
                    self._declared_services(pid), self._self_provides(pid, self._provides(pid))
                )
                if missing:
                    self._blocked[pid] = missing
                    pending.remove(pid)
                    log.warning("plugin_blocked", plugin=pid, missing=missing)
                    continue
                try:
                    await self.load(pid)
                    loaded.append(pid)
                    self._blocked.pop(pid, None)
                    pending.remove(pid)
                    progressed = True
                except Exception as exc:  # noqa: BLE001 - 单个插件坏掉不该拖垮启动
                    self._errors[pid] = f"{type(exc).__name__}: {exc}"
                    pending.remove(pid)
                    log.exception("plugin_load_failed", plugin=pid)
            if not progressed:
                break
        if enabled:
            log.info(
                "plugins_ready",
                loaded=loaded,
                failed=list(self._errors),
                blocked=sorted(self._blocked),
            )
        # 发现得到、却没进 enabled 的插件：不会被加载，频道里勾了也不会响应。
        # 这条日志是「新人卡在 enabled 这步」唯一的主动提示 —— 不报出来就只能靠猜。
        try:
            idle = sorted(set(self.discover()) - set(enabled))
        except Exception:  # noqa: BLE001 - 发现失败不该影响启动
            idle = []
        if idle:
            log.info("plugins_discovered_but_disabled", plugins=idle)
        return loaded

    def _provides(self, pid: str) -> Any:
        """读某插件声明的 provides（只导入类，不构造 context）。"""
        try:
            target, _, _ = self._import_target(pid)
            return getattr(self._pick_plugin(target, pid)(), "provides", None)
        except Exception:  # noqa: BLE001
            return None

    async def resume_blocked(self) -> list[str]:
        """依赖补齐后自动拉起被暂停的插件。返回这次成功恢复的插件。"""
        resumed: list[str] = []
        for pid in list(self._blocked):
            missing = self._service_problems(
                self._declared_services(pid), self._self_provides(pid, self._provides(pid))
            )
            if missing:
                continue
            try:
                await self.load(pid)
                self._blocked.pop(pid, None)
                resumed.append(pid)
                log.info("plugin_resumed", plugin=pid)
            except Exception as exc:  # noqa: BLE001
                self._errors[pid] = f"{type(exc).__name__}: {exc}"
                log.exception("plugin_resume_failed", plugin=pid)
        return resumed

    def blocked(self) -> dict[str, list[str]]:
        """当前因依赖不满足而暂停的插件及其原因。"""
        return {pid: list(reasons) for pid, reasons in self._blocked.items()}

    def _topo_sort(self, ids: list[str]) -> list[str]:
        pending = list(ids)
        done: list[str] = []
        seen: set[str] = set()
        while pending:
            progressed = False
            for pid in list(pending):
                try:
                    target, _, _ = self._import_target(pid)
                    requires = list(self._pick_plugin(target, pid)().manifest().requires)
                except Exception:  # noqa: BLE001
                    requires = []
                if all(r in seen or r not in ids for r in requires):
                    done.append(pid)
                    seen.add(pid)
                    pending.remove(pid)
                    progressed = True
            if not progressed:
                # 环：把还卡着的一起报出来，别静默按原顺序加载 ——
                # 那样依赖会以"偶发的不明 AttributeError"形式出现在运行时。
                raise ContractError(
                    "插件依赖成环：" + " / ".join(sorted(pending)) + "。请检查这些插件的 requires 声明"
                )
        return done

    # -- 服务契约 --------------------------------------------------------- #
    @staticmethod
    def _normalise_provide(cap_name: str, declared: Any) -> tuple[Any, str]:
        """`provides` 的值可以是工厂方法名（老写法），也可以是 ServiceSpec。"""
        if isinstance(declared, ServiceSpec):
            if not declared.factory:
                raise ContractError(
                    f"服务 {cap_name} 的 ServiceSpec 必须写 factory（本类里构造服务的方法名）"
                )
            return declared, declared.factory
        return None, str(declared)

    def _check_declared_services(
        self, pid: str, wanted: dict[str, str], *, self_provides: set[str] | None = None
    ) -> None:
        """核对 `services` 声明：有人提供、版本够。缺一个就明确报错。"""
        problems = self._service_problems(wanted, self_provides or set())
        if problems:
            raise ContractError(
                f"插件 {pid} 的服务依赖不满足："
                + "；".join(problems)
                + "。请把提供方也加进 plugins.enabled，并确保加载顺序（requires）正确"
            )

    def _service_problems(self, wanted: dict[str, str], self_provides: set[str]) -> list[str]:
        """返回缺失/版本不符的说明列表；空列表 = 依赖满足。"""
        problems: list[str] = []
        for name, requirement in (wanted or {}).items():
            if name in self_provides:
                continue
            spec = self.runtime.service_specs.get(name)
            if spec is None:
                owner_hint = ""
                if name in self.runtime.providers:
                    owner_hint = "（提供方没有声明契约，请给它加 ServiceSpec）"
                problems.append(f"{name}：没有插件提供它{owner_hint}")
                continue
            need = parse_version_requirement(requirement)
            if int(getattr(spec, "version", 1)) < need:
                problems.append(f"{name}：需要 v{need}，当前是 v{getattr(spec, 'version', 1)}")
        return problems

    def _declared_services(self, pid: str) -> dict[str, str]:
        """读某插件声明的服务依赖（要导入类，但只是读清单，不构造 context）。"""
        try:
            target, _, _ = self._import_target(pid)
            plugin = self._pick_plugin(target, pid)()
            return dict(getattr(plugin, "services", None) or {})
        except Exception:  # noqa: BLE001 - 损坏的插件交给 load 报错，这里只当没声明
            return {}

    @staticmethod
    def _self_provides(pid: str, provides: Any) -> set[str]:
        return set((provides or {}).keys())

    # -- 规则构造 --------------------------------------------------------- #
    def _build_rule(self, pid: str, func: Any, spec: dict[str, Any]) -> MatchRule:
        kind = spec.get("kind")
        events = spec.get("events")
        if events is None:
            events = {
                "notice": (EventKind.NOTICE,),
                "request": (EventKind.REQUEST,),
                "meta": (EventKind.META,),
            }.get(kind, (EventKind.MESSAGE,))
        platforms = spec.get("platforms")
        kinds = spec.get("channel_kinds")
        return MatchRule(
            plugin_id=pid,
            func=func,
            events=frozenset(events),
            platforms=frozenset(platforms) if platforms else None,
            channel_kinds=frozenset(ChannelKind(k) if isinstance(k, str) else k for k in kinds)
            if kinds
            else None,
            min_role=spec.get("min_role", Role.USER),
            feature=spec.get("feature"),
            commands=tuple(spec.get("commands") or ()),
            pattern=spec.get("pattern"),
            priority=int(spec.get("priority", 0)),
            block=bool(spec.get("block", True)),
            require_at=bool(spec.get("require_at", False)),
            usage=spec.get("usage"),
            description=spec.get("description"),
            timeout=spec.get("timeout"),
            notice_types=spec.get("notice_types"),
            notice_sub_types=spec.get("notice_sub_types"),
            request_types=spec.get("request_types"),
            request_sub_types=spec.get("request_sub_types"),
        )

    def _make_invoker(self, rule: MatchRule, plugin: Plugin, base_ctx: Context) -> Any:
        async def invoker(pctx: Any, outcome: Any) -> bool:
            ctx = Context(
                trace_id=pctx.trace_id,
                plugin_id=rule.plugin_id,
                log=base_ctx.log,
                event=pctx.event,
                reply=pctx.reply,
                config=base_ctx.config,
                net=base_ctx.net,
                storage=base_ctx.storage,
                kv=base_ctx.kv,
                schedule=base_ctx.schedule,
                moderation=base_ctx.moderation,
                introspect=base_ctx.introspect,
                # 服务代理是「每个插件一份」的，必须一起带过来；
                # 漏了它 ctx.service() 就永远说"没声明"。
                services=base_ctx.services,
                handled=pctx.handled,
            )
            kwargs = rule.build_kwargs(outcome, pctx.event.text)
            result = rule.func(ctx, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            if result is False:
                # handler 明确说「这事我不管」：交还给总线，后面的规则继续匹配。
                # 返回 None / 其他值仍视为已承接，老插件行为不变。
                return False
            pctx.handled = True
            return True

        return invoker

    def config_schema(self, pid: str) -> list[dict[str, Any]]:
        """插件声明的可配置项。声明失败/不存在返回空表（控制台退化为自由编辑）。"""
        cached = self._schemas.get(pid)
        if cached is not None:
            return cached
        schema: list[dict[str, Any]] = []
        try:
            target, _, _ = self._import_target(pid)
            schema = list(self._pick_plugin(target, pid).config_schema())
        except Exception:  # noqa: BLE001 - 拿不到声明不该让面板报错
            schema = []
        self._schemas[pid] = schema
        return schema

    # -- 状态 ------------------------------------------------------------- #
    def handles(self) -> dict[str, PluginHandle]:
        return dict(self._handles)

    @property
    def status_errors(self) -> dict[str, str]:
        """加载/依赖层面的错误（面板与 CLI 都要看得见）。"""
        return dict(self._errors)

    def _plugin_classes(self) -> dict[str, type]:
        if self._class_cache is None:
            self._class_cache = {
                str(getattr(cls, "id", "")): cls
                for cls in plugin_sources.iter_plugin_classes(self.runtime.config)
                if getattr(cls, "id", None)
            }
        return self._class_cache

    def status(self) -> list[dict[str, Any]]:
        known = self.discover()
        rows: list[dict[str, Any]] = []
        for pid, source in sorted(known.items()):
            handle = self._handles.get(pid)
            row: dict[str, Any] = {
                "id": pid,
                "source": source,
                "enabled": pid in self.runtime.config.plugins_enabled,
                "loaded": handle is not None,
            }
            if handle is not None:
                row.update(handle.manifest.to_dict())
                row["rules"] = [r.label for r in handle.rules]
            else:
                cls = self._plugin_classes().get(pid)
                if cls is not None:
                    row["name"] = getattr(cls, "name", "") or pid
                    row["version"] = getattr(cls, "version", "0.0.0")
                    row["description"] = getattr(cls, "description", "") or ""
                    row["capabilities"] = list(getattr(cls, "capabilities", None) or [])
            # 插件的私有参数（haiq.toml 的 [plugins.<id>]）+ 它自己声明的表单
            row["config"] = dict(self.runtime.config.plugin_config.get(pid, {}))
            row["config_schema"] = self.config_schema(pid)
            # 依赖关系：谁提供什么服务、这插件又要什么（面板用来画/查依赖）
            plugin_obj = handle.plugin if handle is not None else self._plugin_classes().get(pid)
            row["provides"] = _provides_summary(getattr(plugin_obj, "provides", None))
            row["requires"] = list(getattr(plugin_obj, "requires", None) or [])
            row["services"] = dict(getattr(plugin_obj, "services", None) or {})
            row["secrets"] = _secrets_summary(plugin_obj, row["config"])
            if pid in self._blocked:
                row["blocked"] = list(self._blocked[pid])
            if pid in self._errors:
                row["error"] = self._errors[pid]
            rows.append(row)
        return rows

    def commands(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for handle in self._handles.values():
            for rule in handle.rules:
                if not rule.commands:
                    continue
                out.append(
                    {
                        "plugin": rule.plugin_id,
                        "plugin_name": handle.manifest.name,
                        "command": rule.commands[0],
                        "aliases": list(rule.commands[1:]),
                        "usage": rule.usage,
                        "description": rule.description or handle.manifest.description,
                        "min_role": int(rule.min_role),
                    }
                )
        return sorted(out, key=lambda r: (r["plugin"], r["command"]))
