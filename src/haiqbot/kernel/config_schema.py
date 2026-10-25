"""配置字段的**唯一定义**：标签、控件、取值范围、生效方式。

以前这些信息散在三处：`control.py` 的白名单、前端写死的 input 类型、以及
文档里的表格。改一个字段要改三遍，必然漂移。

现在后端只维护这一张表：
- `control.describe()` 直接吐给前端渲染
- `control.apply_changes()` 用同一份定义做校验
- 「需重启 / 需重连 / 需重载插件」由 `apply` 字段表达，前端据此打徽章

插件私有参数（`[plugins.<id>]`）不在这里 —— 由插件自己的 `config_schema()` 声明。
"""

from __future__ import annotations

from dataclasses import dataclass

# -- 生效方式 --------------------------------------------------------------- #
# hot          保存即生效
# plugin_reload 需要重载相关插件（能力句柄是加载时构造的）
# reconnect    需要断开重连（适配器连接参数）
# restart      需要重启进程（对象在启动时构造，或改了会找不到数据）
APPLY_LABELS = {
    "hot": "即时",
    "plugin_reload": "需重载插件",
    "reconnect": "需重连",
    "restart": "需重启",
}

# -- 分组 ------------------------------------------------------------------- #
GROUP_LABELS = {
    "access": "接入",
    "governance": "治理",
    "media": "媒体",
    "kernel": "内核",
    "service": "服务",
}


@dataclass(frozen=True, slots=True)
class Field:
    section: str  # "" = 顶层
    key: str
    label: str
    kind: str  # str | int | float | bool | enum | list | path | ref
    group: str
    apply: str = "hot"
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    readonly: bool = False
    help: str = ""

    @property
    def path(self) -> str:
        return f"{self.section}.{self.key}" if self.section else self.key

    @property
    def toml_path(self) -> str:
        """落盘用的键路径。顶层字段（`log_level` 等）不带段名。"""
        alias = _FIELD_ALIAS.get(self.path)
        return alias if alias is not None else self.path


SCHEMA: tuple[Field, ...] = (
    # -- 接入：OneBot ------------------------------------------------------- #
    Field("onebot", "enabled", "启用 OneBot", "bool", "access", "restart"),
    Field(
        "onebot",
        "mode",
        "连接角色",
        "enum",
        "access",
        "reconnect",
        choices=("client", "server"),
        help="client = haiqbot 主动连协议端（正向 WS）；server = haiqbot 起 WS 等它连入",
    ),
    Field("onebot", "url", "协议端 WS 地址", "str", "access", "reconnect", help="client 模式使用"),
    Field(
        "onebot",
        "host",
        "本机监听地址（server / http）",
        "str",
        "access",
        "reconnect",
        help="我们起 WS 等协议端连入（server）或起 HTTP 收事件（http）时用；client/sse 模式忽略",
    ),
    Field(
        "onebot",
        "port",
        "本机监听端口（server / http）",
        "int",
        "access",
        "reconnect",
        minimum=1,
        maximum=65535,
        help="同上，与协议端自己的端口无关；client 模式连的是 url，sse 模式走 http_api_url",
    ),
    Field("onebot", "access_token_ref", "Access Token 键名", "ref", "access", "reconnect"),
    Field("onebot", "self_id", "机器人 QQ 号", "str", "access", "restart", help="留空则自动探测"),
    Field("onebot", "api_timeout_seconds", "控制类 API 超时", "float", "access", minimum=1, maximum=120),
    Field(
        "onebot",
        "send_timeout_seconds",
        "发送超时（含媒体）",
        "float",
        "access",
        minimum=5,
        maximum=600,
        help="大图 base64 上传要留足时间",
    ),
    Field(
        "onebot",
        "image_transport",
        "图片投递方式",
        "enum",
        "access",
        choices=("auto", "base64", "url"),
        help="auto = 有本地文件就内嵌，否则退 URL",
    ),
    Field("onebot", "image_max_bytes", "内嵌上限（字节）", "int", "access", minimum=100000, maximum=50000000),
    # -- 接入：QQ 官方 ------------------------------------------------------ #
    Field("official", "enabled", "启用 QQ 官方", "bool", "access", "restart"),
    Field("official", "app_id", "AppID", "str", "access", "restart"),
    Field("official", "app_secret_ref", "AppSecret 键名", "ref", "access", "restart"),
    Field(
        "official",
        "intents",
        "事件订阅掩码",
        "int",
        "access",
        "restart",
        choices=("33554432", "33556416", "33556417"),
        help="33554432 = 1<<25，覆盖 C2C 与群 @ 消息",
    ),
    Field("official", "api_base", "API 基地址", "str", "access"),
    Field("official", "token_url", "取 Token 地址", "str", "access"),
    # -- 治理：配额 --------------------------------------------------------- #
    Field("quota", "enabled", "启用配额", "bool", "governance"),
    Field(
        "quota",
        "per_user_per_day",
        "单用户日配额（0 = 不限制）",
        "int",
        "governance",
        minimum=0,
        maximum=100000,
    ),
    Field(
        "quota",
        "per_channel_per_day",
        "单群日配额（0 = 不限制）",
        "int",
        "governance",
        minimum=0,
        maximum=1000000,
    ),
    Field(
        "quota",
        "rate_per_user_per_min",
        "单用户每分钟限流（0 = 不限制）",
        "int",
        "governance",
        minimum=0,
        maximum=10000,
    ),
    Field(
        "quota",
        "rate_global_per_min",
        "全局每分钟限流（0 = 不限制）",
        "int",
        "governance",
        minimum=0,
        maximum=100000,
    ),
    Field("quota", "overflow_message", "配额用尽提示", "str", "governance"),
    # -- 治理：策略 --------------------------------------------------------- #
    Field(
        "policy", "handle_timeout_seconds", "处理器默认超时", "float", "governance", minimum=0.5, maximum=600
    ),
    Field("policy", "max_handlers_per_event", "单事件最多处理器", "int", "governance", minimum=1, maximum=20),
    Field("policy", "owners", "所有者 ID", "list", "governance", help="拥有最高权限"),
    Field("policy", "admins", "管理员 ID", "list", "governance"),
    Field(
        "policy", "channel_whitelist", "频道白名单", "list", "governance", help="留空 = 不限制（全部放行）"
    ),
    Field("policy", "channel_blacklist", "频道黑名单", "list", "governance", help="优先于白名单"),
    # -- 治理：出站闸门 ----------------------------------------------------- #
    Field("outbound", "enabled", "启用出站闸门", "bool", "governance", help="关掉后发送不受限，仅调试用"),
    Field(
        "outbound",
        "min_interval_seconds",
        "同频道最小间隔（秒）",
        "float",
        "governance",
        minimum=0,
        maximum=60,
        help="两条消息之间至少隔这么久，防连发",
    ),
    Field(
        "outbound",
        "global_per_minute",
        "全局每分钟上限",
        "int",
        "governance",
        minimum=0,
        maximum=600,
        help="0 = 不限，对应“这个号总共别太快”",
    ),
    Field(
        "outbound",
        "max_wait_seconds",
        "排队上限（秒）",
        "float",
        "governance",
        minimum=1,
        maximum=600,
        help="排不到就丢这一条，避免回复越排越旧",
    ),
    Field("outbound", "retry_max", "限速重试次数", "int", "governance", minimum=0, maximum=10),
    Field(
        "outbound",
        "retry_backoff_seconds",
        "重试退避基数（秒）",
        "float",
        "governance",
        minimum=0,
        maximum=60,
        help="指数退避：1x / 2x / 4x …",
    ),
    # -- 媒体 --------------------------------------------------------------- #
    Field("media", "dir", "存储目录", "path", "media", "restart", readonly=True),
    Field("media", "ttl_seconds", "保留时长（秒）", "int", "media", minimum=60, maximum=31536000),
    Field("media", "gc_interval_seconds", "清理间隔（秒）", "int", "media", minimum=30, maximum=86400),
    Field("media", "public_base_url", "公网基地址", "str", "media", help="留空 = 用面板内网地址"),
    # -- 内核 --------------------------------------------------------------- #
    Field("webui", "enabled", "启用控制台", "bool", "kernel", "restart", readonly=True),
    Field("webui", "host", "监听地址", "str", "kernel", "restart", readonly=True),
    Field("webui", "port", "监听端口", "int", "kernel", "restart", readonly=True),
    Field(
        "webui",
        "write_enabled",
        "允许面板写操作",
        "bool",
        "kernel",
        help="关掉后本页所有保存会失败 —— 等于把自己锁在外面",
    ),
    Field("webui", "token_ref", "写操作令牌键名", "ref", "kernel"),
    Field(
        "log",
        "level",
        "日志级别",
        "enum",
        "kernel",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    ),
    Field("log", "json", "JSON 日志", "bool", "kernel"),
    Field("path", "data_dir", "数据目录", "path", "kernel", "restart", readonly=True),
    Field("path", "db_path", "数据库路径", "path", "kernel", "restart", readonly=True),
    Field("path", "secrets_dir", "凭据目录", "path", "kernel", "restart", readonly=True),
    Field("path", "plugin_dirs", "外部插件目录", "list", "kernel", "restart", readonly=True),
)

BY_PATH: dict[str, Field] = {f.path: f for f in SCHEMA}

# 只在这些地方出现的「段名 → 真实配置属性名」映射。
# 因为 `log_level` / `log_json` 在 HaiqConfig 上是顶层字段，
# 而 `data_dir` / `db_path` / `secrets_dir` / `plugin_dirs` 也是顶层。
_SECTION_ATTR = {
    "log": "",  # 顶层：log_level / log_json
    "path": "",  # 顶层：data_dir / db_path / ...
}
_FIELD_ALIAS = {
    "log.level": "log_level",
    "log.json": "log_json",
    "path.data_dir": "data_dir",
    "path.db_path": "db_path",
    "path.secrets_dir": "secrets_dir",
    "path.plugin_dirs": "plugin_dirs",
}


def read_value(config: object, field: Field) -> object:
    """按 Field 从 HaiqConfig 上取值。"""
    alias = _FIELD_ALIAS.get(field.path)
    if alias is not None:
        return getattr(config, alias, None)
    section = getattr(config, field.section, None)
    return getattr(section, field.key, None) if section is not None else None


def write_value(config: object, field: Field, value: object) -> None:
    """把值写回 HaiqConfig。"""
    alias = _FIELD_ALIAS.get(field.path)
    if alias is not None:
        setattr(config, alias, value)
        return
    section = getattr(config, field.section, None)
    if section is None:
        raise KeyError(f"配置段不存在：{field.section}")
    setattr(section, field.key, value)


def group_of(field: Field) -> str:
    return GROUP_LABELS.get(field.group, field.group)
