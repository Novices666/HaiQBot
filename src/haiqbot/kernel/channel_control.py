"""频道开关的**唯一写入口**。

三个入口共用这里，保证"一份数据 + 一套校验 + 一次审计 + 一次缓存失效"：

    面板  POST /api/channels/{channel}
    CLI   haiq channels enable|disable
    群内  /haiqbot 开启|关闭              ← 内核系统命令（S2 落地）

数据仍存在 `channel_config` 表（唯一真相）。`plugins` 列是**已开启项**列表，
支持两种写法：

    "painter"        整个插件
    "chat:ask"       只开 chat 的 ask 功能

默认语义（2026-10 决策）：**没有记录 = 什么都没开**。所以这里的写入永远显式，
不做"空 = 不限"的推断 —— 那个推断会让人分不清"没配"和"配了但空"。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from haiqbot.kernel.log import get_logger

log = get_logger(__name__)

FEATURE_SEP = ":"


class ChannelControlError(Exception):
    """写频道配置时的可读错误（调用方转 400）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def split_target(raw: str) -> tuple[str, str | None]:
    """`painter:draw` → ("painter", "draw")；`painter` → ("painter", None)。"""
    text = (raw or "").strip()
    if not text:
        raise ChannelControlError("开关目标不能为空")
    if FEATURE_SEP in text:
        pid, _, feature = text.partition(FEATURE_SEP)
        pid, feature = pid.strip(), feature.strip()
        if not pid or not feature:
            raise ChannelControlError(f"目标格式不对：{raw}（应为 插件 或 插件:功能）")
        return pid, feature
    return text, None


def normalise(targets: Iterable[str]) -> list[str]:
    """去空、去重、稳定排序 —— 保证面板/CLI/群命令写出来的结果一致。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in targets:
        pid, feature = split_target(raw)
        key = f"{pid}{FEATURE_SEP}{feature}" if feature else pid
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return sorted(out)


def _known_plugins(runtime: Any, registry: Any) -> set[str]:
    if registry is None:
        return set()
    try:
        return set(registry.discover())
    except Exception:  # noqa: BLE001 - 发现失败不该让面板 500
        return set()


def validate(runtime: Any, registry: Any, targets: Iterable[str]) -> list[str]:
    """校验并规范化。未知插件直接报错 —— 静默忽略会让运维以为写成功了。"""
    items = normalise(targets)
    known = _known_plugins(runtime, registry)
    for item in items:
        pid, _feature = split_target(item)
        if known and pid not in known:
            raise ChannelControlError(f"没有插件 {pid}（已发现：{', '.join(sorted(known))}）")
    return items


async def current(runtime: Any, channel: str) -> list[str]:
    """本群当前开启项；没有记录 = 空（默认关）。"""
    row = await runtime.store.channel_get(channel)
    if not row:
        return []
    items = row.get("plugins") or []
    if isinstance(items, str):  # 兜底：早期版本可能存成 JSON 字符串
        try:
            items = json.loads(items)
        except Exception:  # noqa: BLE001
            items = []
    return [str(x) for x in items if str(x).strip()]


async def set_features(
    runtime: Any,
    registry: Any,
    channel: str,
    *,
    on: Iterable[str] = (),
    off: Iterable[str] = (),
    replace: Iterable[str] | None = None,
    actor: str = "cli",
) -> dict[str, Any]:
    """改本群开关。`replace` 优先于 `on/off`。

    返回 `{channel, enabled, changed}`，`changed` 为实际变化项（供审计与提示）。
    """
    if not channel or not channel.strip():
        raise ChannelControlError("频道标识不能为空")
    channel = channel.strip()

    before = await current(runtime, channel)
    if replace is not None:
        after = validate(runtime, registry, replace)
    else:
        adding = validate(runtime, registry, on)
        removing = set(validate(runtime, registry, off))
        after = sorted({x for x in before if x not in removing} | set(adding))

    changed = sorted(set(after) ^ set(before))
    if not changed:
        return {"channel": channel, "enabled": after, "changed": []}

    await runtime.store.channel_upsert(
        channel,
        enabled=1,
        plugins=json.dumps(after, ensure_ascii=False),
    )
    gate = getattr(runtime, "channel_gate", None)
    if gate is not None:
        gate.invalidate(channel)

    await _audit(runtime, channel, before, after, actor)
    log.info("channel_features_changed", channel=channel, enabled=len(after), actor=actor)
    return {"channel": channel, "enabled": after, "changed": changed}


async def _audit(runtime: Any, channel: str, before: list[str], after: list[str], actor: str) -> None:
    try:
        await runtime.audit.record(
            trace_id=None,
            plugin_id=None,
            action="channel:features",
            channel=channel,
            actor=actor,
            detail=json.dumps({"before": before, "after": after}, ensure_ascii=False)[:800],
        )
    except Exception:  # noqa: BLE001 - 审计失败不阻断开关本身
        log.warning("channel_audit_failed", channel=channel)


async def list_all(runtime: Any) -> list[dict[str, Any]]:
    """所有已配置频道（列表页用）。"""
    rows = await runtime.store.channel_all()
    return list(rows or [])


# -- 体检 ------------------------------------------------------------------ #
#
# 「默认全关 + feature 级开关」换来了安全，也换来一个新的坑：
# **配了却不生效**。插件名拼错、feature 名拼错、enabled=1 但列表为空、
# 插件是 always 不需要开关……这些都不报错，只是安静地什么都不做。
# verify() 把它们一次性点出来，让人不用靠"发条消息试试"来排障。


def known_features(registry: Any) -> dict[str, set[str]]:
    """{插件 id: 该插件声明过的 feature}。只有能拿到运行时 handle 时才准。"""
    out: dict[str, set[str]] = {}
    handles = getattr(registry, "handles", None)
    if not callable(handles):
        return out  # CLI 的静态注册表没有 handles：跳过 feature 级校验，不误报
    try:
        items = list(handles().values())
    except Exception:  # noqa: BLE001
        return out
    for handle in items:
        pid = _plugin_id(handle)
        if not pid:
            continue
        bucket = out.setdefault(pid, set())
        for rule in getattr(handle, "rules", ()) or ():
            feature = getattr(rule, "feature", None)
            if feature:
                bucket.add(str(feature))
    return out


def _plugin_id(handle: Any) -> str | None:
    for candidate in (
        getattr(getattr(handle, "manifest", None), "id", None),
        getattr(getattr(handle, "plugin", None), "id", None),
    ):
        if candidate:
            return str(candidate)
    return None


def gating_map(registry: Any) -> dict[str, str]:
    """{插件 id: opt_in | always}。

    只有两态：`opt_in`（默认，必须在频道名单里才跑）和 `always`（治理类插件，免疫开关）。
    曾计划过第三态 `opt_out`（默认开、按需禁用），但它与「默认全关」的核心语义直接冲突
    —— 一个"默认就开"的插件意味着新群不配置也会响应，所以没有实现，文档也不该再提。
    """
    out: dict[str, str] = {}
    handles = getattr(registry, "handles", None)
    if not callable(handles):
        return out
    try:
        items = list(handles().values())
    except Exception:  # noqa: BLE001
        return out
    for handle in items:
        pid = _plugin_id(handle)
        if pid:
            out[pid] = str(getattr(getattr(handle, "plugin", None), "gating", "opt_in"))
    return out


def _enabled_plugins(runtime: Any) -> set[str] | None:
    """读全局启用清单 `[plugins] enabled`。

    读不到（CLI 的桩 runtime、配置尚未加载）就返回 `None`，调用方据此**跳过**检查 ——
    宁可少报一条，也不要在拿不到真相时乱报 error。
    """
    cfg = getattr(runtime, "config", None)
    if cfg is None:
        return None
    try:
        raw = getattr(cfg, "plugins_enabled", None)
    except Exception:  # noqa: BLE001 - 配置对象坏了不该让面板 500
        return None
    if raw is None:
        return None
    try:
        return {str(x) for x in raw if str(x).strip()}
    except TypeError:
        return None


async def verify(runtime: Any, registry: Any) -> list[dict[str, Any]]:
    """体检所有频道配置。返回 `[{channel, level, code, message}]`，level ∈ error/warn/info。"""
    problems: list[dict[str, Any]] = []
    known = _known_plugins(runtime, registry)
    feats = known_features(registry)
    gating = gating_map(registry)
    # 全局启用清单读得到才做「未启用」检查；读不到就不猜。
    enabled_set = _enabled_plugins(runtime)

    def add(channel: str, level: str, code: str, message: str) -> None:
        problems.append({"channel": channel, "level": level, "code": code, "message": message})

    for row in await list_all(runtime):
        channel = str(row.get("channel") or "?")
        enabled = bool(row.get("enabled"))
        items = [str(x) for x in (row.get("plugins") or []) if str(x).strip()]

        if not enabled:
            add(channel, "info", "channel_disabled", "enabled=0：整条频道关闭，列表里的功能也不会响应")
            continue
        if not items:
            add(channel, "warn", "nothing_enabled", "enabled=1 但功能列表为空 —— 该频道实际仍然什么都不做")
            continue

        for item in items:
            try:
                pid, feature = split_target(item)
            except ChannelControlError as exc:
                add(channel, "error", "bad_target", f"{item}：{exc.message}")
                continue

            if known and pid not in known:
                add(
                    channel,
                    "error",
                    "unknown_plugin",
                    f"{pid}：没有这个插件（已发现：{', '.join(sorted(known)) or '无'}）",
                )
                continue
            if enabled_set is not None and pid not in enabled_set:
                add(
                    channel,
                    "error",
                    "plugin_not_enabled",
                    f"{pid}：插件在磁盘上，但没写进 [plugins] enabled —— 不会被加载，"
                    f"这里配了也不会响应（已启用：{', '.join(sorted(enabled_set)) or '无'}）",
                )
                continue
            if gating.get(pid) == "always":
                add(channel, "info", "always_on", f"{pid}：always 插件，本来就与开关无关（写在这里是多余的）")
                continue
            if not feature:
                continue

            available = feats.get(pid, set())
            if not available:
                add(
                    channel,
                    "warn",
                    "plugin_declares_no_feature",
                    f"{item}：{pid} 没有声明任何 feature，写成 {pid} 即可",
                )
            elif feature not in available:
                add(
                    channel,
                    "error",
                    "unknown_feature",
                    f"{item}：{pid} 没有这个功能（可用：{', '.join(sorted(available))}）",
                )

    # -- 全局检查（不属于任何一条频道，channel 记为 "*"）-------------------- #
    # 这是「配了却没反应」的另一半：频道里勾了、却没进 enabled 只能在逐频道
    # 循环里查；而"磁盘上有、却没人用"只有全局视角才看得见。
    if known and enabled_set is not None:
        idle = sorted(known - enabled_set)
        if idle:
            add(
                "*",
                "info",
                "plugin_disabled",
                f"{len(idle)} 个插件在磁盘上但未启用：{', '.join(idle)} —— 不会被加载，"
                f"任何频道都不会响应（要启用就加进 [plugins] enabled）",
            )
        missing = sorted(enabled_set - known)
        if missing:
            add(
                "*",
                "error",
                "enabled_plugin_missing",
                f"[plugins] enabled 里的 {', '.join(missing)} 找不到对应插件 —— "
                f"名字打错，或目录/包装卸了（已发现：{', '.join(sorted(known)) or '无'}）",
            )
    return problems
