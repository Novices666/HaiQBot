"""风控插件：刷屏限流 / 广告链接 / 违禁词拦截 / 入群欢迎。

**为什么 `gating = "always"`**

频道开关默认全关，这对业务插件是对的；但治理型插件（风控、审计、限流）必须无视开关 ——
否则一个"被关掉的群"里谁在刷屏、谁在发广告，我们完全看不见，
那等于先关掉监控再谈治理。`always` 就是为这类插件准备的豁免口。

**为什么 handler 返回 False 而不是 None**

`kernel/bus.py::_dispatch` 的语义：
- 返回 `False` → 「匹配但放行」：不计消费、不占名额，后面的插件照常执行；
- 返回 `True` → 承接：`handler_count + 1`，默认 `max_handlers = 1`，后续规则不再跑。
所以风控必须显式 `return False` 放行，否则它会把整条消息吃掉。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any

from haiqbot.plugin_api import Context, Plugin, Role, on_message, on_notice

#: 广告/引流链接特征（够用就好，不追求完备 —— 误伤由管理员豁免兜底）
_LINK_RE = re.compile(
    r"(https?://|www\.|t\.me/|qm\.qq\.com/|\b[\w-]+\.(com|cn|net|org|xyz|top|vip|cc|io)(/|\b))",
    re.I,
)

DEFAULTS: dict[str, Any] = {
    "window_seconds": 10,
    "max_messages": 5,
    "block_links": True,
    "blocked_words": "",
    "warn_text": "⚠️ 慢一点：{seconds} 秒内最多 {limit} 条",
    "block_text": "🚫 已拦截：{reason}",
    "exempt_admins": True,
    "welcome": "",
}


class Guard(Plugin):
    id = "guard"
    name = "风控"
    description = "刷屏限流 / 广告链接 / 违禁词拦截（always：不受频道开关约束）"
    gating = "always"
    capabilities = ["kv"]

    def __init__(self) -> None:
        # 统计放内存：面板要立刻读到，且不该为了一次展示去查库。
        # 重启即清零 —— 这是个"监控计数器"，不是账本。
        self._stats: dict[str, Any] = {"total": 0, "by_reason": {}, "recent": []}

    @classmethod
    def config_schema(cls) -> list[dict[str, Any]]:
        return [
            {"key": "window_seconds", "label": "限流时间窗（秒）", "kind": "int", "default": 10},
            {"key": "max_messages", "label": "窗口内最多几条", "kind": "int", "default": 5},
            {"key": "block_links", "label": "拦截广告链接", "kind": "bool", "default": True},
            {
                "key": "blocked_words",
                "label": "违禁词（逗号分隔）",
                "kind": "str",
                "default": "",
                "description": "留空 = 不按词拦截",
            },
            {"key": "warn_text", "label": "限流提示语", "kind": "str", "default": DEFAULTS["warn_text"]},
            {
                "key": "block_text",
                "label": "拦截提示语",
                "kind": "str",
                "default": DEFAULTS["block_text"],
                "description": "{reason} 会被替换成拦截原因；留空 = 不提示",
            },
            {
                "key": "exempt_admins",
                "label": "管理员免检",
                "kind": "bool",
                "default": True,
                "description": "关掉之后管理员也会被拦（自测拦截效果时用）",
            },
            {
                "key": "welcome",
                "label": "入群欢迎语",
                "kind": "str",
                "default": "",
                "description": "留空 = 不欢迎；always 生效，未配置的群也会发",
            },
        ]

    # -- 配置 --------------------------------------------------------------- #
    def _cfg(self, ctx: Context) -> dict[str, Any]:
        merged = dict(DEFAULTS)
        raw = getattr(ctx, "config", None) or {}
        # ctx.config 是 LiveConfig（支持热更新的 Mapping），**不是 dict** ——
        # 用 isinstance(raw, dict) 判断会静默回落默认值，改配置等于没改。
        if isinstance(raw, Mapping):
            merged.update({k: v for k, v in raw.items() if v is not None})
        return merged

    # -- 主判定 ------------------------------------------------------------- #
    @on_message(priority=1000)
    async def watch(self, ctx: Context) -> bool | None:
        text = getattr(ctx.event, "text", "") or ""
        if not text.strip():
            return False  # 纯图片/空消息不归风控管

        cfg = self._cfg(ctx)
        # 管理员及以上免检（可关）：误伤时必须留一条人工通道，
        # 但自测拦截效果时要能把这条通道临时关掉。
        if cfg["exempt_admins"] and ctx.event.actor.role >= Role.ADMIN:
            return False

        reason: str | None = None

        if cfg["block_links"] and _LINK_RE.search(text):
            reason = "广告链接"
        else:
            words = [w.strip() for w in str(cfg["blocked_words"]).split(",") if w.strip()]
            hit = next((w for w in words if w in text), None)
            if hit:
                reason = f"违禁词「{hit}」"
            elif await self._too_fast(ctx, cfg):
                reason = "刷屏"

        if reason is None:
            return False  # 放行：不计消费，后面插件照常跑

        self._record(ctx, reason, text)
        await self._notify(ctx, cfg, reason)
        ctx.log.info("guard_blocked", reason=reason, actor=ctx.event.actor.id, channel=str(ctx.event.channel))
        return True  # 拦截：消费这条消息

    # -- 限流 --------------------------------------------------------------- #
    def _flood_key(self, ctx: Context) -> str:
        return f"flood:{ctx.event.channel}:{ctx.event.actor.id}"

    async def _too_fast(self, ctx: Context, cfg: dict[str, Any]) -> bool:
        if ctx.kv is None:
            return False
        now = time.time()
        window = float(cfg["window_seconds"])
        limit = int(cfg["max_messages"])
        if window <= 0 or limit <= 0:
            return False
        key = self._flood_key(ctx)
        raw = await ctx.kv.get(key, "[]")
        try:
            stamps = [float(x) for x in json.loads(raw)]
        except (ValueError, TypeError):
            stamps = []
        fresh = [t for t in stamps if now - t <= window]
        fresh.append(now)
        await ctx.kv.set(key, json.dumps(fresh[-64:]))
        return len(fresh) > limit

    async def _notify(self, ctx: Context, cfg: dict[str, Any], reason: str) -> None:
        """拦截时回一句，同一人 30 秒内只提示一次 —— 不然拦截本身就变成刷屏。"""
        if ctx.kv is not None:
            key = f"warned:{ctx.event.channel}:{ctx.event.actor.id}"
            now = time.time()
            raw = await ctx.kv.get(key, "0")
            try:
                last = float(raw or 0)
            except (TypeError, ValueError):
                last = 0.0
            if now - last < 30:
                return
            await ctx.kv.set(key, str(now))

        template = cfg["warn_text"] if reason == "刷屏" else cfg["block_text"]
        if not str(template or "").strip():
            return
        try:
            await ctx.reply.text(
                str(template).format(reason=reason, seconds=cfg["window_seconds"], limit=cfg["max_messages"])
            )
        except Exception:  # noqa: BLE001 - 提示发不出去不影响拦截
            pass

    # -- 统计 --------------------------------------------------------------- #
    def _record(self, ctx: Context, reason: str, text: str) -> None:
        self._stats["total"] += 1
        by_reason = self._stats["by_reason"]
        by_reason[reason] = by_reason.get(reason, 0) + 1
        recent = self._stats["recent"]
        recent.insert(
            0,
            {
                "ts": int(time.time()),
                "channel": str(ctx.event.channel),
                "actor": ctx.event.actor.id,
                "reason": reason,
                "text": text[:60],
            },
        )
        del recent[10:]

    # -- 入群欢迎（顺带验证 always 在"未配置的群"也生效）--------------------- #
    @on_notice(priority=1000)
    async def greet(self, ctx: Context) -> bool | None:
        if str(getattr(ctx.event, "notice_type", "")) != "group_increase":
            return False
        cfg = self._cfg(ctx)
        text = str(cfg.get("welcome") or "").strip()
        if not text:
            return False
        await ctx.reply.text(text)
        return True

    # -- 面板 --------------------------------------------------------------- #
    def panel_meta(self) -> dict[str, Any]:
        return {"title": "风控 · 拦截统计", "order": 10}

    def panel_page(self, panel: Any) -> dict[str, Any]:
        cfg: dict[str, Any] = dict(DEFAULTS)
        raw = getattr(panel, "config", None) or {}
        if isinstance(raw, Mapping):
            cfg.update({k: v for k, v in raw.items() if v is not None})
        rows = [
            {
                "reason": reason,
                "count": count,
            }
            for reason, count in sorted(self._stats["by_reason"].items(), key=lambda kv: -kv[1])
        ] or [{"reason": "（还没有拦截）", "count": 0}]
        return {
            "title": "风控",
            "sections": [
                {
                    "type": "text",
                    "text": "这个页面由 guard 插件自己声明。它是 gating=always 的插件，"
                    "即使某个群没在频道开关里配置，风控照样生效 —— 拦截会记在这里。",
                },
                {
                    "type": "field_group",
                    "title": "当前策略",
                    "fields": [
                        {"label": "限流窗口", "value": f"{cfg['window_seconds']} 秒"},
                        {"label": "窗口内上限", "value": f"{cfg['max_messages']} 条"},
                        {"label": "拦截广告链接", "value": "是" if cfg["block_links"] else "否"},
                        {"label": "违禁词", "value": cfg["blocked_words"] or "（无）"},
                        {"label": "累计拦截", "value": self._stats["total"]},
                        {"label": "管理员豁免", "value": "是（Role.ADMIN 及以上）"},
                    ],
                },
                {
                    "type": "readonly_table",
                    "title": "按原因统计",
                    "columns": [{"key": "reason", "title": "原因"}, {"key": "count", "title": "次数"}],
                    "rows": rows,
                },
                {
                    "type": "readonly_table",
                    "title": "最近 10 条",
                    "columns": [
                        {"key": "time", "title": "时间"},
                        {"key": "channel", "title": "频道"},
                        {"key": "actor", "title": "触发者"},
                        {"key": "reason", "title": "原因"},
                        {"key": "text", "title": "内容"},
                    ],
                    "rows": [
                        {
                            "time": time.strftime("%H:%M:%S", time.localtime(item["ts"])),
                            "channel": item["channel"],
                            "actor": item["actor"],
                            "reason": item["reason"],
                            "text": item["text"],
                        }
                        for item in self._stats["recent"]
                    ]
                    or [{"time": "—", "channel": "—", "actor": "—", "reason": "（空）", "text": ""}],
                },
                {"type": "action_button", "label": "清零统计", "action": "reset", "confirm": "确定清零？"},
            ],
        }

    def panel_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action == "reset":
            self._stats = {"total": 0, "by_reason": {}, "recent": []}
            return {"message": "统计已清零"}
        raise KeyError(action)
