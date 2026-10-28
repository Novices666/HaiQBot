"""插件抽象与内核暴露给插件的唯一入口 `Context`。

**安全要害**：`Context` 是 frozen-ish 的数据对象，字段白名单里没有任何密钥。
插件想用凭据，只能通过内核发放的句柄（`ctx.net` 等）或 `ctx.service()` 拿服务；
凭据只在提供服务的那一侧、由内核注入的解析器取。
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

from haiqbot.core.channel import ChannelKind, Role
from haiqbot.core.errors import CapabilityDenied, CapabilityUnavailable
from haiqbot.core.event import Event, EventKind
from haiqbot.core.reply import Reply

PLUGIN_API_VERSION = 1


class LiveConfig(MutableMapping[str, Any]):
    """`ctx.config` 的活视图：读写直通 `runtime.config.plugin_config[pid]`。

    **为什么不是一份 dict**：以前 `registry.load()` 会对配置做一次 `dict()` 拷贝，
    于是「面板改了参数、运行中的插件看不到」，必须两边同步才生效 —— 这是一个
    必然会被踩的坑。改成视图后全进程只有一份数据。

    写入只落在内存里（与旧行为一致），落盘由控制面负责。
    """

    __slots__ = ("_store", "_pid")

    def __init__(self, store: dict[str, dict[str, Any]], pid: str) -> None:
        self._store = store
        self._pid = pid

    def _bag(self) -> dict[str, Any]:
        return self._store.setdefault(self._pid, {})

    def __getitem__(self, key: str) -> Any:
        return self._bag()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._bag()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._bag()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._bag())

    def __len__(self) -> int:
        return len(self._bag())

    def __repr__(self) -> str:
        return f"LiveConfig({self._pid!r}, {dict(self._bag())!r})"


class SecretValue(str):
    """防误用的字符串包装。

    只防「顺手打印」：print / f-string / repr / 日志都显示占位符。
    不防「故意转义」（切片、join、json.dumps 仍拿到真值）—— 这正好对齐
    L1 的语义：默认不主动给明文，而不是物理隔离。
    """

    __slots__ = ("label",)

    def __new__(cls, raw: str, label: str) -> SecretValue:
        obj = super().__new__(cls, raw)
        obj.label = label
        return obj

    @property
    def raw(self) -> str:
        """唯一取真值的正当方式。"""
        return str.__str__(self)

    def __repr__(self) -> str:
        return f"<secret:{self.label}>"

    def __str__(self) -> str:
        return f"<secret:{self.label}>"

    def __format__(self, spec: str) -> str:
        return f"<secret:{self.label}>"


@dataclass(frozen=True)
class SecretSlot:
    """插件声明「我要一个凭据」。

    `ref` 省略时键名由内核推导为 `haiqbot.{plugin_id}.{slot_name}`；
    写死 ref 可以绑到已有键名（迁移期很有用）。
    """

    label: str
    required: bool = True
    ref: str | None = None
    description: str = ""
    group: str | None = None


def resolve_secret_ref(plugin_id: str, slot_name: str, slot: SecretSlot, cfg: Mapping[str, Any]) -> str:
    """槽位 -> 物理键名。覆盖优先，其次插件显式 ref，最后推导。"""
    override = cfg.get(f"{slot_name}_ref")
    if isinstance(override, str) and override:
        return override
    if slot.ref:
        return slot.ref
    return f"haiqbot.{plugin_id}.{slot_name}"


class SecretHandle:
    """惰性凭据句柄。

    每次调用都重新解析 vault（vault 自己有 mtime 缓存），所以**改凭据之后
    下一次请求就生效**，不需要重载插件。
    """

    __slots__ = ("_plugin_id", "_name", "_slot", "_cfg", "_vault", "_audit", "_reads")

    def __init__(
        self,
        plugin_id: str,
        name: str,
        slot: SecretSlot,
        cfg: Mapping[str, Any],
        vault: Any,
        audit: Any = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._name = name
        self._slot = slot
        self._cfg = cfg
        self._vault = vault
        self._audit = audit
        self._reads = 0

    # -- 元信息（不取值） -------------------------------------------------- #
    @property
    def name(self) -> str:
        return self._name

    @property
    def label(self) -> str:
        return self._slot.label

    @property
    def required(self) -> bool:
        return self._slot.required

    @property
    def physical_ref(self) -> str:
        return resolve_secret_ref(self._plugin_id, self._name, self._slot, self._cfg)

    def is_set(self) -> bool:
        """只问「有没有」，不把明文带进插件。

        `VaultError`（密码错 / 文件损坏）必须向上抛 —— 那是"读不了"，
        不是"没配"。吞掉它会把排查引向错误方向。
        """
        if self._vault is None:
            return False
        return bool(self._vault.resolve(self.physical_ref).present)

    # -- 常用鉴权形态 ------------------------------------------------------ #
    def headers(self, *, header: str = "Authorization", scheme: str = "Bearer") -> dict[str, str]:
        value = self.require().raw
        return {header: f"{scheme} {value}".strip() if scheme else value}

    def query(self, param: str = "key") -> dict[str, str]:
        return {param: self.require().raw}

    # -- 明文逃生舱（非 HTTP 场景：HMAC / 数据库 / PEM …） ----------------- #
    def text(self) -> SecretValue | None:
        """明文逃生舱。首次与每 100 次调用会记一条 `secret_plain_read` 审计。"""
        if self._vault is None:
            return None
        value = self._vault.get(self.physical_ref)
        if not value:
            return None
        self._reads += 1
        self._audit_plain_read()
        return SecretValue(value, self._slot.label)

    def _audit_plain_read(self) -> None:
        if self._audit is None:
            return
        if self._reads != 1 and self._reads % 100 != 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 同步上下文（CLI / 测试）不记
            return
        detail = json.dumps(
            {"slot": self._name, "ref": self.physical_ref, "reads": self._reads},
            ensure_ascii=False,
        )
        try:
            loop.create_task(
                self._audit.record(
                    trace_id=None,
                    plugin_id=self._plugin_id,
                    action="secret_plain_read",
                    actor="plugin",
                    detail=detail,
                )
            )
        except Exception:  # noqa: BLE001 - 审计失败不该影响业务
            pass

    def require(self) -> SecretValue:
        value = self.text()
        if value is None:
            raise CapabilityUnavailable(f"缺少凭据「{self._slot.label}」（{self.physical_ref}）")
        return value

    def bytes(self) -> bytes:
        """二进制用途（HMAC / AES）。hex 与 base64 形态一并登记进脱敏名单。"""
        raw = self.require().raw
        encoded = raw.encode("utf-8")
        try:
            from .log import register_secret

            register_secret(encoded.hex())
            register_secret(base64.b64encode(encoded).decode("ascii"))
        except Exception:  # noqa: BLE001
            pass
        return encoded

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.require().raw)


@dataclass
class ProviderContext:
    """提供给「能力提供者」的构造上下文。

    凭据入口只有 `slot(name)`：插件在 `secrets` 里声明过哪些槽位，就能取哪些。
    """

    plugin_id: str
    config: MutableMapping[str, Any]
    media: Any = None
    log: Any = None
    slots: dict[str, SecretHandle] = field(default_factory=dict)

    def slot(self, name: str) -> SecretHandle:
        """取自己声明过的凭据槽位。没声明就是越权，直接拒。"""
        handle = self.slots.get(name)
        if handle is None:
            raise CapabilityDenied(f"插件 {self.plugin_id} 没有声明凭据槽位 {name} —— 请先在 secrets 里声明")
        return handle


@dataclass(slots=True)
class PluginManifest:
    id: str
    name: str = ""
    version: str = "0.0.0"
    api_version: int = PLUGIN_API_VERSION
    isolation: str = "inline"
    capabilities: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    description: str = ""
    source: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "isolation": self.isolation,
            "capabilities": list(self.capabilities),
            "requires": list(self.requires),
            "description": self.description,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """`provides` 的契约声明。

    为什么要有它：`provides` 如果只是"能力名 → 工厂方法名"，消费方就是在赌
    "那个实例上到底有哪个方法"。改个签名不会有任何报错，要等线上 AttributeError。
    把方法名与版本写进契约后，提供方一加载就校验，消费方一加载就核对。
    """

    name: str
    # 本类里构造这个服务的方法名（不能自动推导，写清楚比猜好）
    factory: str = ""
    version: int = 1
    methods: tuple[str, ...] = ()
    doc: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "factory": self.factory,
            "version": self.version,
            "methods": list(self.methods),
            "doc": self.doc,
        }


def parse_version_requirement(text: str) -> int:
    """把 `"1"` / `">=2"` / `"^3"` 统一成最低整数版本。"""
    cleaned = str(text).strip().lstrip("^~")
    if cleaned.startswith(">="):
        cleaned = cleaned[2:]
    try:
        return int(cleaned)
    except ValueError:
        return 0


class ServiceProxy:
    """插件看到的服务句柄。

    每次取属性都重新解析 —— 提供方被重载/卸掉时自动失效，插件不用自己刷新引用。
    """

    __slots__ = ("_name", "_resolver")

    def __init__(self, name: str, resolver: Any) -> None:
        self._name = name
        self._resolver = resolver

    def _target(self) -> Any:
        service = self._resolver(self._name)
        if service is None:
            raise CapabilityUnavailable(f"服务当前不可用：{self._name}")
        return service

    def __getattr__(self, item: str) -> Any:
        return getattr(self._target(), item)

    def __repr__(self) -> str:
        return f"<ServiceProxy {self._name!r}>"


@dataclass
class Context:
    """插件看到的全部世界。

    没有 `vault`、没有 `env`、没有 `secrets` —— 这不是疏忽，是设计。
    """

    trace_id: str
    plugin_id: str
    log: Any
    event: Event | None = None
    reply: Reply | None = None
    config: MutableMapping[str, Any] = field(default_factory=dict)
    net: Any = None
    storage: Any = None
    kv: Any = None
    schedule: Any = None
    # 群管理动作（撤回/禁言/踢人/成员列表/审批）。平台不支持时方法会抛错，
    # 先用 `ctx.moderation.supports("mute")` 探测。
    moderation: Any = None
    # 平台查询与轻量动作（查群/查消息/上传文件/表情回应）
    platform: Any = None
    introspect: Any = None
    # 已声明的插件服务：name -> ServiceProxy（只含 `services` 里声明过的）
    services: dict[str, Any] = field(default_factory=dict)
    handled: bool = False

    def service(self, name: str) -> Any:
        """取一个已声明的插件服务。

        没在 `services` 里声明过就抛 `CapabilityDenied` —— 和"能力"同一套边界：
        想用别人提供的服务，先在清单上写清楚。
        """
        proxy = self.services.get(name)
        if proxy is None:
            raise CapabilityDenied(f"插件 {self.plugin_id} 没有声明服务 {name}，请先在 services 里声明")
        return proxy

    @property
    def text(self) -> str:
        return self.event.text if self.event is not None else ""

    def __repr__(self) -> str:
        return f"<Context plugin={self.plugin_id!r} trace={self.trace_id!r}>"


# --------------------------------------------------------------------------- #
# 装饰器：声明「我关心什么」
# --------------------------------------------------------------------------- #
def _add_spec(func: Any, spec: dict[str, Any]) -> Any:
    specs = getattr(func, "_haiqbot_specs", None)
    if specs is None:
        specs = []
        func._haiqbot_specs = specs
    specs.append(spec)
    return func


def on_command(
    name: str,
    *,
    aliases: tuple[str, ...] | list[str] = (),
    timeout: float | None = None,
    priority: int = 0,
    block: bool = True,
    require_at: bool = False,
    usage: str | None = None,
    description: str | None = None,
    min_role: Role = Role.USER,
    platforms: tuple[str, ...] | list[str] | None = None,
    channel_kinds: tuple[ChannelKind, ...] | list[ChannelKind] | None = None,
    feature: str | None = None,
) -> Any:
    """命令触发器。兼容 `/画图` 与 `画图` 两种写法。"""

    def deco(func: Any) -> Any:
        return _add_spec(
            func,
            {
                "kind": "command",
                "commands": (name, *aliases),
                "timeout": timeout,
                "priority": priority,
                "block": block,
                "require_at": require_at,
                "usage": usage,
                "description": description,
                "feature": feature,
                "min_role": min_role,
                "platforms": platforms,
                "channel_kinds": channel_kinds,
            },
        )

    return deco


def on_message(
    *,
    priority: int = -50,
    timeout: float | None = None,
    block: bool = False,
    require_at: bool = False,
    min_role: Role = Role.USER,
    platforms: tuple[str, ...] | list[str] | None = None,
    channel_kinds: tuple[ChannelKind, ...] | list[ChannelKind] | None = None,
    feature: str | None = None,
) -> Any:
    """任意消息触发器（无命令词约束）。默认不阻断后续匹配。"""

    def deco(func: Any) -> Any:
        return _add_spec(
            func,
            {
                "kind": "message",
                "timeout": timeout,
                "priority": priority,
                "block": block,
                "require_at": require_at,
                "feature": feature,
                "min_role": min_role,
                "platforms": platforms,
                "channel_kinds": channel_kinds,
            },
        )

    return deco


def on_regex(
    pattern: str,
    *,
    priority: int = 0,
    timeout: float | None = None,
    block: bool = True,
    require_at: bool = False,
    min_role: Role = Role.USER,
    feature: str | None = None,
    flags: int = 0,
) -> Any:
    """正则触发器。handler 可声明 `match` 形参拿到 Match 对象。"""
    import re as _re

    compiled = _re.compile(pattern, flags)

    def deco(func: Any) -> Any:
        return _add_spec(
            func,
            {
                "kind": "regex",
                "pattern": compiled,
                "timeout": timeout,
                "priority": priority,
                "block": block,
                "require_at": require_at,
                "feature": feature,
                "min_role": min_role,
                "platforms": None,
                "channel_kinds": None,
            },
        )

    return deco


_StrOrSeq = str | tuple[str, ...] | list[str] | None


def _as_frozenset(value: _StrOrSeq) -> frozenset[str] | None:
    """把 `"a"` / `("a","b")` / None 统一成 frozenset；None 表示不挑。"""
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    return frozenset(str(v) for v in value)


def on_notice(
    *,
    notice_type: _StrOrSeq = None,
    sub_type: _StrOrSeq = None,
    priority: int = 0,
    block: bool = True,
    timeout: float | None = None,
    min_role: Role = Role.USER,
    platforms: tuple[str, ...] | list[str] | None = None,
    channel_kinds: tuple[ChannelKind, ...] | list[ChannelKind] | None = None,
    feature: str | None = None,
) -> Any:
    """通知类事件（入群/退群、撤回、禁言、管理员变更、戳一戳……）。

        @on_notice(notice_type="group_increase")
        async def welcome(self, ctx, event): ...

    `notice_type` / `sub_type` 都可以给字符串或列表；不给 = 全都接。
    `min_role` 卡的是**事件当事人**的角色，不是操作者 —— 判"这个人能不能
    触发我的逻辑"用它，判"谁干的"用 `event.operator_id`。
    """

    def deco(func: Any) -> Any:
        return _add_spec(
            func,
            {
                "kind": "notice",
                "timeout": timeout,
                "priority": priority,
                "block": block,
                "require_at": False,
                "feature": feature,
                "min_role": min_role,
                "platforms": platforms,
                "channel_kinds": channel_kinds,
                "events": (EventKind.NOTICE,),
                "notice_types": _as_frozenset(notice_type),
                "notice_sub_types": _as_frozenset(sub_type),
            },
        )

    return deco


def on_request(
    *,
    request_type: _StrOrSeq = None,
    sub_type: _StrOrSeq = None,
    priority: int = 0,
    block: bool = True,
    timeout: float | None = None,
    min_role: Role = Role.ADMIN,
    feature: str | None = None,
    platforms: tuple[str, ...] | list[str] | None = None,
) -> Any:
    """请求类事件：加好友、入群申请、群邀请。

    默认 `min_role=ADMIN`：**审批是特权操作**，不写权限就等于没权限。
    要做"谁都能申请"的入口，请在 handler 里自己读 `event.actor.id` 判断。
    """

    def deco(func: Any) -> Any:
        return _add_spec(
            func,
            {
                "kind": "request",
                "timeout": timeout,
                "priority": priority,
                "block": block,
                "require_at": False,
                "feature": feature,
                "min_role": min_role,
                "platforms": platforms,
                "channel_kinds": None,
                "events": (EventKind.REQUEST,),
                "request_types": _as_frozenset(request_type),
                "request_sub_types": _as_frozenset(sub_type),
            },
        )

    return deco


def on_meta(*, priority: int = 0, block: bool = False, timeout: float | None = None) -> Any:
    """元事件。当前只有 OneBot 的 `lifecycle`（连上 / 启用 / 停用）。"""

    def deco(func: Any) -> Any:
        return _add_spec(
            func,
            {
                "kind": "meta",
                "timeout": timeout,
                "priority": priority,
                "block": block,
                "require_at": False,
                "min_role": Role.USER,
                "platforms": None,
                "channel_kinds": None,
                "events": (EventKind.META,),
            },
        )

    return deco


# --------------------------------------------------------------------------- #
# 插件基类
# --------------------------------------------------------------------------- #
class Plugin:
    """所有插件继承它。类属性即清单。"""

    id: str = ""
    name: str = ""
    version: str = "0.1.0"
    api_version: int = PLUGIN_API_VERSION
    isolation: str = "inline"
    capabilities: list[str] = []
    # 凭据槽位：声明「我要什么」，键名由内核推导或覆盖
    secrets: dict[str, SecretSlot] = {}
    # 插件级依赖（只影响加载顺序；缺了也只是顺序不对，不会拦加载）
    requires: list[str] = []
    # 服务级依赖（硬约束）：服务名 → 最低版本要求，如 {"economy": "1", "quiz": ">=2"}
    services: dict[str, str] = {}
    description: str = ""
    # 本插件**提供**的能力：能力名 → 本类的工厂方法名。
    # 内核据此把实现登记进 runtime.providers，消费方调能力时惰性解析。
    # 例：provides = {"image:generate": "build_image_service"}
    provides: dict[str, str] = {}

    def __init__(self) -> None:
        self.ctx: Context | None = None
        self.config: MutableMapping[str, Any] = {}

    # -- 生命周期 --------------------------------------------------------- #
    async def on_load(self, ctx: Context) -> None:
        """加载钩子。可以在这里读 kv、注册定时任务。"""

    async def on_unload(self) -> None:
        """卸载钩子。默认无操作。"""

    # -- 内省 ------------------------------------------------------------- #
    def manifest(self, source: str = "builtin") -> PluginManifest:
        if not self.id:
            raise ValueError(f"{type(self).__name__} 必须定义类属性 id")
        return PluginManifest(
            id=self.id,
            name=self.name or self.id,
            version=self.version,
            api_version=self.api_version,
            isolation=self.isolation,
            capabilities=tuple(self.capabilities),
            requires=tuple(self.requires),
            description=self.description or (type(self).__doc__ or "").strip(),
            source=source,
        )

    @classmethod
    def config_schema(cls) -> list[dict[str, Any]]:
        """声明本插件的可配置项，供控制台「插件设置」页渲染表单。

        每项形如::

            {"key": "ack_text", "label": "回执文案", "kind": "str",
             "default": "🤔 思考中…", "description": "…", "choices": []}

        `kind` 取 ``str`` / ``int`` / ``float`` / ``bool``。
        不声明时控制台退化为「自由键值编辑」，仍然可改 `[plugins.<id>]`。
        """
        return []

    async def config_options(self, key: str, pctx: ProviderContext) -> Any:
        """按需算「某个配置字段有哪些可选值」（例如模型列表要现问接口）。

        和 `config_schema()` 的分工：
        - `config_schema()` 是**静态**声明（标签、类型、默认值），不碰网络；
        - 这里是**动态**取值，会走网络/读 vault，所以只在面板点「拉取」时调用。

        返回 `["a", "b"]` 或 `[{"value": "a", "label": "A（说明）"}]`；
        返回 None / 未实现 = 该字段没有动态来源，面板沿用静态 `choices`。

        `pctx` 与能力工厂拿到的是同一种上下文：能读自己的 `config`、
        能通过 `slot(name)` 取自己声明过的凭据 —— 拿不到别人的。
        """
        return None

    def iter_matchers(self) -> list[tuple[Any, dict[str, Any]]]:
        """收集本插件所有被装饰的 handler。"""
        found: list[tuple[Any, dict[str, Any]]] = []
        for attr_name in dir(self):
            if attr_name.startswith("__"):
                continue
            try:
                member = getattr(self, attr_name)
            except Exception:  # noqa: BLE001 - 属性可能抛错，忽略
                continue
            if not inspect.ismethod(member):
                continue
            specs = getattr(member, "_haiqbot_specs", None)
            if not specs:
                continue
            for spec in specs:
                found.append((member, spec))
        return found
