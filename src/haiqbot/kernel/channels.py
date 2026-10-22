"""群/私聊维度的配置闸门（`channel_config` 表）。

定位
----
全局配置（`[policy]` / `[quota]`）回答"整个号怎么运作"；这里回答
"**这个群**要不要理、只理哪些插件、这个群给多少额度"。

三个开关
--------
- `enabled = 0`：整个群静默。托管着几十个群时，踢掉一个闹事的群不该去改
  全局白名单（一改就影响所有群）。
- `plugins = [...]`：本群只允许这些插件生效。空 = 不限制。
- `quota_user` / `quota_chan`：覆盖全局的每日配额（NULL = 用全局值）。

缓存
----
每个群一次事件读一次库太浪费，所以按 `ttl`（默认 5s）缓存一行；
面板写完调 `invalidate()` 立刻生效。
"""

from __future__ import annotations

import time
from typing import Any

from haiqbot.kernel.log import get_logger

log = get_logger(__name__)


class ChannelGate:
    """`channel_config` 的读取闸门。"""

    def __init__(self, store: Any, *, ttl: float = 5.0) -> None:
        self.store = store
        self.ttl = ttl
        self._cache: dict[str, tuple[float, dict[str, Any] | None]] = {}

    def invalidate(self, channel: str | None = None) -> None:
        if channel is None:
            self._cache.clear()
        else:
            self._cache.pop(channel, None)

    async def row(self, channel: str) -> dict[str, Any] | None:
        """取这个频道的配置行；没配过返回 None（= 一切按全局来）。"""
        if self.store is None:
            return None
        now = time.monotonic()
        hit = self._cache.get(channel)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        try:
            data = await self.store.channel_get(channel)
        except Exception:  # noqa: BLE001 - 读配置失败按"没配过"处理，别拦消息
            log.warning("channel_config_read_failed", channel=channel)
            data = None
        self._cache[channel] = (now, data)
        return data

    async def quota_overrides(self, channel: str) -> dict[str, int] | None:
        row = await self.row(channel)
        if not row:
            return None
        out: dict[str, int] = {}
        for key, col in (("per_user_per_day", "quota_user"), ("per_channel_per_day", "quota_chan")):
            value = row.get(col)
            if value is not None:
                out[key] = int(value)
        return out or None

    async def allowed_plugins(self, channel: str) -> set[str]:
        """本群已开启项（plugin 或 plugin:feature）。

        默认语义是**全关**：没有记录、或记录里为空，都返回空集。
        调用方（_mw_scope）据此决定"这条消息没人接"，而不是"不限"。
        """
        row = await self.row(channel)
        if not row:
            return set()
        plugins = row.get("plugins") or []
        if isinstance(plugins, str):  # 兜底：某些实现在这里存的是 JSON 字符串
            import json

            try:
                plugins = json.loads(plugins) or []
            except Exception:  # noqa: BLE001
                plugins = []
        return {str(p) for p in plugins if str(p).strip()}
