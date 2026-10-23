"""配置模型与加载。

设计原则：**配置文件里永远不出现密钥**。
需要凭据的字段一律只放「vault 键名」（`*_ref`），真值由 vault 在运行时注入。
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from haiqbot.core.errors import ConfigError

DEFAULT_CONFIG_FILE = "haiq.toml"


class QuotaCfg(BaseModel):
    enabled: bool = True
    per_user_per_day: int = 20
    per_channel_per_day: int = 300
    rate_per_user_per_min: int = 6
    rate_global_per_min: int = 120
    overflow_message: str = "配额已用尽，明天再来吧~"


class PolicyCfg(BaseModel):
    """访问控制与分发策略。"""

    channel_whitelist: list[str] = Field(default_factory=list)
    channel_blacklist: list[str] = Field(default_factory=list)
    owners: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)
    handle_timeout_seconds: float = 8.0
    max_handlers_per_event: int = 1


class MediaCfg(BaseModel):
    dir: Path = Path("data/media")
    ttl_seconds: int = 86400
    gc_interval_seconds: int = 600
    # 只读媒体服务：给协议端（可能跑在容器里）一个能拉到图片的地址。
    # 只暴露 `/i/<id>`，不暴露面板；serve_port=0 表示关闭。
    serve_host: str = "0.0.0.0"
    serve_port: int = 8090
    # 对外可访问的基地址（反代域名）。为空则用 webui 的内网地址。
    public_base_url: str | None = None


class WebuiCfg(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8080

    # 是否开放面板上的写操作（改配置 / 启停插件 / 重载）。
    # 关掉之后所有 POST 一律 403，面板退回纯只读仪表盘。
    write_enabled: bool = True
    # 写操作令牌所在的 vault 键名（令牌真值只存 vault，绝不落配置文件）。
    # 该键在 vault 中不存在时，写操作一律 403，并提示用 `haiq webui token --generate` 生成。
    token_ref: str = "WEFT_WEBUI_TOKEN"


class VaultCfg(BaseModel):
    """凭据存储。所有平台同一套实现 —— 不做平台区分。"""

    # None = 用 HaiqConfig.secrets_dir（仍可被 WEFT_SECRETS_DIR 覆盖）
    dir: Path | None = None
    # aead = scrypt + AES-256-GCM（可迁移到任意机器）；none = 明文（显式降级）
    enc: str = "aead"
    # 密码所在的环境变量名。密码真值只在该环境变量里，不落配置文件。
    passphrase_ref: str = "WEFT_VAULT_PASSPHRASE"


class OfficialCfg(BaseModel):
    enabled: bool = False
    app_id: str | None = None
    app_secret_ref: str = "WEFT_QQ_APP_SECRET"
    # intents 1<<25 覆盖 C2C_MESSAGE_CREATE 与 GROUP_AT_MESSAGE_CREATE
    intents: int = 1 << 25
    api_base: str = "https://api.sgroup.qq.com"
    token_url: str = "https://bots.qq.com/app/getAppAccessToken"


class OneBotCfg(BaseModel):
    enabled: bool = False
    # client: haiqbot 主动连接 OneBot 端的 WS 服务（NapCat 正向 WS）
    # server: 我们起 WS 服务，等 OneBot 端反向连入
    # http:   API 走 HTTP POST 到协议端，事件由我们起 HTTP 服务接收（反向 HTTP 上报）
    # sse:    API 走 HTTP POST，事件用 SSE 订阅（不需要任何入站端口）
    mode: Literal["client", "server", "http", "sse"] = "client"
    url: str = "ws://127.0.0.1:3001"
    host: str = "127.0.0.1"
    port: int = 3002
    # mode=http / sse 时协议端的 HTTP API 基址（NapCat 的 http server 端口）
    http_api_url: str = "http://127.0.0.1:3000"
    # mode=http：我们接收事件上报的路径与可选 HMAC-SHA1 密钥槽位
    webhook_path: str = "/onebot"
    # 留空 = 不校验 X-Signature（此时退化成校验 Bearer token）
    webhook_secret_ref: str = ""
    # mode=sse：事件流路径（go-cqhttp / NapCat 都是 /_events）
    sse_path: str = "/_events"
    # mode=sse：SSE 完整地址。留空 = {http_api_url}{sse_path}。
    # NapCat 的 HTTP API(3000) 与 SSE(3003) 是**两个端口**，这时必须显式填。
    sse_url: str = ""
    access_token_ref: str = "WEFT_ONEBOT_ACCESS_TOKEN"
    self_id: str | None = None
    # 控制类 API（get_*）的超时
    api_timeout_seconds: float = 15.0
    # 携带媒体的发送（send_*_msg + 大图 base64）要额外留上传时间
    send_timeout_seconds: float = 120.0
    # 图片投递方式：
    #   auto   —— 有本地文件就内嵌 base64（不依赖公网），否则退 URL
    #   base64 —— 强制内嵌，读不到文件时退 URL 并告警
    #   url    —— 只发 URL（要求平台能访问到）
    image_transport: Literal["auto", "base64", "url"] = "auto"
    # 内嵌上限，超过则改发 URL（QQ 单图上限约 30MB，留余量）
    image_max_bytes: int = 20_000_000


class OutboundCfg(BaseModel):
    """出站闸门：管的是"这个号总共发多快"，不是业务配额。

    配额管"用户能用几次"；这一层管"别被 QQ 风控"。两者别混。
    """

    enabled: bool = True
    # 同一个群/私聊两条消息之间的最小间隔（秒）
    min_interval_seconds: float = 1.2
    # 全局每分钟最多发几条（0 = 不限）
    global_per_minute: int = 30
    # 排队超过这个时间就放弃这一条（防止队列越堆越旧）
    max_wait_seconds: float = 30.0
    # 被频控时的重试次数与退避
    retry_max: int = 3
    retry_backoff_seconds: float = 2.0
    retry_max_backoff_seconds: float = 20.0


class HaiqConfig(BaseModel):
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_json: bool = False
    db_path: Path = Path("data/haiq.db")

    # 内核不认识任何具体插件：默认一个都不启用，由 haiq.toml 显式列出。
    plugins_enabled: list[str] = Field(default_factory=list)
    # 本地目录插件：`plugins/` 是框架目录，`plugins-extra/` 是你自己的目录。
    # 不存在的目录会被静默跳过，所以全新环境只建 `plugins/` 也能跑。
    # 相对路径按进程工作目录解析。
    plugin_dirs: list[Path] = Field(default_factory=lambda: [Path("plugins"), Path("plugins-extra")])
    # 每个插件的参数，取自 haiq.toml 的 [plugins.<id>]
    plugin_config: dict[str, dict[str, Any]] = Field(default_factory=dict)

    quota: QuotaCfg = Field(default_factory=QuotaCfg)
    policy: PolicyCfg = Field(default_factory=PolicyCfg)
    media: MediaCfg = Field(default_factory=MediaCfg)
    webui: WebuiCfg = Field(default_factory=WebuiCfg)
    official: OfficialCfg = Field(default_factory=OfficialCfg)
    onebot: OneBotCfg = Field(default_factory=OneBotCfg)
    outbound: OutboundCfg = Field(default_factory=OutboundCfg)
    vault: VaultCfg = Field(default_factory=VaultCfg)
    secrets_dir: Path = Path("secrets")

    # 配置文件落盘位置（由 load_config 填入，供面板持久化用）。
    # 不写进 haiq.toml，只是内存里的一个指针。
    config_path: Path | None = None


def _env_override(raw: dict[str, Any]) -> dict[str, Any]:
    """少量顶层字段支持 WEFT_* 环境变量覆盖，便于容器化部署。"""
    if v := os.environ.get("WEFT_DATA_DIR"):
        raw["data_dir"] = v
    if v := os.environ.get("WEFT_LOG_LEVEL"):
        raw["log_level"] = v
    if v := os.environ.get("WEFT_LOG_JSON"):
        raw["log_json"] = v.lower() in {"1", "true", "yes", "on"}
    if v := os.environ.get("WEFT_SECRETS_DIR"):
        raw["secrets_dir"] = v
    return raw


# 旧版把生图 / 对话配置放在内核顶层段，现在归插件所有。
# 这是**历史配置迁移映射**，不是内核依赖插件：删掉它只会让老配置静默失效，
# 所以留着；新增插件不需要在这里登记任何东西。
_LEGACY_SECTIONS: dict[str, str] = {"image": "painter", "chat": "chat"}


def _absorb_legacy_sections(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把 `[image]` / `[chat]` 并进 `[plugins.painter]` / `[plugins.chat]`。

    返回被搬走的原始段（用于改写文件），空 dict 表示没发生迁移。
    """
    moved: dict[str, dict[str, Any]] = {}
    plugin_cfg = raw.setdefault("plugin_config", {})
    for section, pid in _LEGACY_SECTIONS.items():
        block = raw.pop(section, None)
        if not isinstance(block, dict):
            continue
        moved[section] = block
        target = plugin_cfg.setdefault(pid, {})
        for key, value in block.items():
            target.setdefault(key, value)  # 插件段里已经写了的值优先
    return moved


def _rewrite_legacy_sections(path: Path, moved: dict[str, dict[str, Any]]) -> None:
    """把配置文件里的旧段搬到插件段，并留一份 `.bak`。

    注意：`[plugins.<id>]` 可能**已经存在**（比如面板改过 ack_text），
    所以必须逐键写进去，不能直接追加一个新表 —— 那会生成重复 TOML 表。
    """
    from haiqbot.kernel.config_writer import set_value, take_section

    original = path.read_text(encoding="utf-8")
    text = original
    for section, pid in _LEGACY_SECTIONS.items():
        block = moved.get(section)
        if not block:
            continue
        _, text = take_section(text, section)
        for key, value in block.items():
            text = set_value(text, f"plugins.{pid}", key, value)
    if text == original:
        return
    path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
    path.write_text(text, encoding="utf-8")


def _read_toml(path: Path) -> dict[str, Any]:
    """读 TOML：容忍 BOM（见 kernel.toml_io），并把解析失败翻成人话。

    裸的 `TOMLDecodeError` 对新用户毫无帮助，尤其「文件带 BOM」这种在 Windows 上
    极易发生、又完全看不出异常的情况。
    """
    from haiqbot.kernel.toml_io import has_bom, loads

    try:
        return loads(path.read_bytes())
    except tomllib.TOMLDecodeError as exc:
        hint = "文件里混入了非法字符，或手动改动时写坏了某个值。"
        if has_bom(path):
            hint = "文件带 UTF-8 BOM（BOM 已自动剥离，说明另有语法错误）。"
        raise ConfigError(
            f"{path} 不是合法的 TOML：{exc}\n  可能原因：{hint}\n  "
            f"想从零开始：删掉 {path} 后重新执行 `haiq init`。"
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} 不是 UTF-8 编码（{exc.reason}）。\n  请用支持 UTF-8 的编辑器另存为「UTF-8」后重试。"
        ) from exc


def _warn_misplaced_fields(raw: dict[str, Any], path: Path) -> None:
    """顶层字段被写进某个 `[section]` 时告警。

    TOML 里 `plugin_dirs = [...]` 一旦写在任何 `[section]` 之后，就会变成**段内键**，
    被 pydantic 静默忽略。实测踩过：本地目录插件整套体系无声失效，而报错为零。
    """
    names = set(HaiqConfig.model_fields)
    for section, block in raw.items():
        if section == "plugins" or not isinstance(block, dict):
            continue
        for key in block:
            if key in names:
                from haiqbot.kernel.log import get_logger

                get_logger(__name__).warning(
                    "config_field_misplaced",
                    field=key,
                    section=section,
                    path=str(path),
                    hint=f"`{key}` 是顶层字段，必须写在第一个 [section] 之前",
                )


def load_config(path: str | Path | None = None) -> HaiqConfig:
    """从 haiq.toml 加载配置；文件不存在时返回全默认值。"""
    cfg_path = Path(path) if path else Path(DEFAULT_CONFIG_FILE)
    raw: dict[str, Any] = {}
    if cfg_path.is_file():
        raw = _read_toml(cfg_path)
        _warn_misplaced_fields(raw, cfg_path)

    # [plugins.<id>] 与顶层 plugins_enabled 分开存放
    plugins_table = raw.pop("plugins", {}) or {}
    if isinstance(plugins_table, dict):
        enabled = plugins_table.pop("enabled", None)
        if isinstance(enabled, list):
            raw["plugins_enabled"] = enabled
        raw["plugin_config"] = plugins_table

    raw = _env_override(raw)
    moved = _absorb_legacy_sections(raw)
    if moved and cfg_path.is_file():
        try:
            _rewrite_legacy_sections(cfg_path, moved)
        except Exception as exc:  # noqa: BLE001 - 迁移失败不该阻止启动
            from haiqbot.kernel.log import get_logger

            get_logger(__name__).warning("legacy_config_migration_failed", error=type(exc).__name__)
    cfg = HaiqConfig.model_validate(raw)
    cfg.config_path = cfg_path
    return cfg


def _toml_value(value: Any) -> str:
    """把 Python 值渲染成 TOML 字面量（字符串走 JSON 转义，TOML 兼容）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)


def _plugin_schemas() -> dict[str, list[dict[str, Any]]]:
    """扫描已安装插件声明的 `config_schema()`。

    只通过 entry point 发现 —— 内核不认识任何具体插件包名。
    """
    from haiqbot.kernel.plugin_sources import iter_plugin_classes

    out: dict[str, list[dict[str, Any]]] = {}
    for cls in iter_plugin_classes():
        pid = getattr(cls, "id", None)
        if not pid or pid in out:
            continue
        try:
            schema = cls.config_schema()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(schema, list) and schema:
            out[str(pid)] = schema
    return out


def _render_plugin_sections() -> str:
    """按每个插件自己的 `config_schema()` 生成 `[plugins.<id>]` 段。"""
    schemas = _plugin_schemas()
    if not schemas:
        return "# （未发现可生成默认值的插件）"
    blocks: list[str] = []
    for pid in sorted(schemas):
        block = [f"[plugins.{pid}]"]
        for field in schemas[pid]:
            key = field.get("key")
            if not key or "default" not in field:
                continue
            if key.endswith("_ref"):
                block.append("# 这是 vault 里的键名，不是密钥")
            block.append(f"{key} = {_toml_value(field['default'])}")
        blocks.append("\n".join(block))
    return "\n\n".join(blocks)


def dump_default_config() -> str:
    """生成 `haiqbot init` 用的 haiq.toml 模板（不含任何密钥）。

    插件清单**不写死**：按当前环境实际能发现到的插件生成。内核里出现具体插件
    id 就等于「内核认识插件」，插件一搬走这份模板立刻撒谎。
    """
    from .kernel.plugin_sources import default_plugin_dirs, iter_plugin_classes

    try:
        found = sorted({str(getattr(cls, "id", "") or "") for cls in iter_plugin_classes()} - {""})
    except Exception:  # noqa: BLE001 - 发现失败就不填，别让 init 崩
        found = []
    enabled_literal = "[" + ", ".join(f'"{pid}"' for pid in found) + "]"
    dirs_literal = "[" + ", ".join(f'"{Path(p).as_posix()}"' for p in default_plugin_dirs()) + "]"

    tpl = """# haiqbot 配置 —— 请勿在此文件中写入任何密钥，密钥一律用 `haiq vault set` 管理。

data_dir = "data"
log_level = "INFO"
log_json = false

# 本地目录插件。必须写在任何 [section] 之前 —— 写进段内会被静默忽略。
# 相对路径按启动时的工作目录解析，所以起服务前先 cd 到项目根。
plugin_dirs = __PLUGIN_DIRS__

[plugins]
# 内核不认识任何具体插件。这份清单是生成配置时**实际发现到的插件**；
# 若为空，说明当时的工作目录下没有 plugins/ —— 请到项目根再 init，或自行填入 id。
enabled = __PLUGINS_ENABLED__

# 每个插件的私有参数放在 [plugins.<id>] 下，例如：
# [plugins.painter]
# max_per_user_per_day = 20

[quota]
enabled = true
per_user_per_day = 20
per_channel_per_day = 300
rate_per_user_per_min = 6
rate_global_per_min = 120

[policy]
# 留空表示不限制
channel_whitelist = []
channel_blacklist = []
owners = []
admins = []

[media]
dir = "data/media"
ttl_seconds = 86400
# 公网反代基地址，例如 "https://bot.example.com"
# public_base_url = ""

[outbound]
# 出站闸门：防止高频发送被 QQ 风控。管的是"这个号发多快"，不是"用户能用几次"。
enabled = true
# 同一个群/私聊两条消息之间的最小间隔（秒）
min_interval_seconds = 1.2
# 全局每分钟最多发几条（0 = 不限）
global_per_minute = 30
# 排队超过这个时长就丢掉这一条，避免队列越堆越旧
max_wait_seconds = 30
# 被平台明确限速时重试几次（超时不会重试 —— 超时可能已经送达）
retry_max = 3
retry_backoff_seconds = 2

[webui]
enabled = true
host = "127.0.0.1"
port = 8080
# 是否允许通过面板改配置 / 启停插件。关掉即退回纯只读仪表盘。
write_enabled = true
# 写操作令牌的 vault 键名（令牌真值只存 vault），生成：haiq webui token --generate
token_ref = "WEFT_WEBUI_TOKEN"

# ── 插件配置 ───────────────────────────────────────────────────────────────
# 生图 / 对话的**实现归插件所有**，参数也在这里，不再是内核的 [image] / [chat]。
# 拔掉对应插件 = 能力消失，消费方自动降级。

__PLUGIN_SECTIONS__

[official]
enabled = false
# app_id = ""
app_secret_ref = "WEFT_QQ_APP_SECRET"

[onebot]
enabled = false
mode = "client"   # client / server / http / sse
http_api_url = "http://127.0.0.1:3000"   # mode=http 或 sse 时协议端的 HTTP API
webhook_path = "/onebot"                 # mode=http 收上报的路径
webhook_secret_ref = ""                  # 留空 = 退化成校验 Bearer token
sse_path = "/_events"                    # mode=sse 的事件流路径
url = "ws://127.0.0.1:3001"
access_token_ref = "WEFT_ONEBOT_ACCESS_TOKEN"
# 图片怎么发：auto（有本地文件就内嵌 base64，无需公网）/ base64 / url
image_transport = "auto"
"""
    return (
        tpl.replace("__PLUGIN_SECTIONS__", _render_plugin_sections())
        .replace("__PLUGINS_ENABLED__", enabled_literal)
        .replace("__PLUGIN_DIRS__", dirs_literal)
    )
