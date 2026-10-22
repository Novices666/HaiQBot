"""会话与参与者标识。

`ChannelId` 是「往哪儿发」的唯一地址；`Actor` 是「谁发的」。
两者都不含平台特有结构，adapter 负责在边界处转换。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class ChannelKind(StrEnum):
    GROUP = "group"
    PRIVATE = "private"
    GUILD_CHANNEL = "guild_channel"
    GUILD_DM = "guild_dm"
    UNKNOWN = "unknown"


class Role(IntEnum):
    """权限等级，数值越大越高。"""

    USER = 1
    ADMIN = 2
    OWNER = 3


@dataclass(frozen=True, slots=True)
class ChannelId:
    """platform + 类型 + 平台内 ID。

    字符串形式：``platform:kind:id`` 或 ``platform:kind:id:sub``，可用于配置白名单。
    """

    platform: str
    kind: ChannelKind
    id: str
    sub: str | None = None

    def __str__(self) -> str:
        base = f"{self.platform}:{self.kind.value}:{self.id}"
        return f"{base}:{self.sub}" if self.sub else base

    @classmethod
    def parse(cls, raw: str) -> ChannelId:
        parts = raw.split(":")
        if len(parts) < 3:  # noqa: PLR2004
            raise ValueError(f"非法 ChannelId 字符串：{raw!r}")
        platform, kind, ident = parts[0], parts[1], parts[2]
        sub = parts[3] if len(parts) > 3 else None  # noqa: PLR2004
        return cls(platform=platform, kind=ChannelKind(kind), id=ident, sub=sub)

    def matches(self, pattern: str) -> bool:
        """支持 `platform:kind:id` 精确匹配与 `platform:kind:*` 通配。"""
        if pattern == "*":
            return True
        pat = pattern.split(":")
        mine = [self.platform, self.kind.value, self.id]
        for idx, chunk in enumerate(pat):
            if chunk == "*":
                continue
            if idx >= len(mine) or chunk != mine[idx]:
                return False
        return True


@dataclass(frozen=True, slots=True)
class Actor:
    id: str
    name: str | None = None
    role: Role = Role.USER
    is_bot: bool = False

    def __str__(self) -> str:
        return self.name or self.id
