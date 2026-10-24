"""事件模型。

`MessageEvent` 是绝大多数插件唯一关心的类型。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .channel import Actor, ChannelId
from .segment import Segment, has_at_bot, segments_to_text


class EventKind(StrEnum):
    MESSAGE = "message"
    NOTICE = "notice"
    REQUEST = "request"
    META = "meta"


def _raw_ts(raw: dict[str, Any]) -> str:
    """上报里的 `time`（OneBot 是秒级整数）。

    拿不到就返回空串 —— 空 key 的意思是「无法判重，别拦」，宁可重复也别误杀。
    """
    if not isinstance(raw, dict):
        return ""
    ts = raw.get("time")
    return str(ts) if ts is not None else ""


@dataclass(slots=True)
class Event:
    kind: EventKind
    channel: ChannelId
    actor: Actor
    ts: float = field(default_factory=time.time)
    trace_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return ""

    @property
    def text(self) -> str:
        return ""

    def dedupe_key(self) -> str:
        """幂等 key：同一事件被平台重复上报时用它去重。空串 = 不参与去重。

        为什么要按事件类型分开算：`msg_id` 只在消息里唯一；`request` 有平台发的
        `flag`；`notice` 大多只有组合字段 —— 用一个通用 key 只会误杀。
        """
        return ""


@dataclass(slots=True)
class MessageEvent(Event):
    segments: list[Segment] = field(default_factory=list)
    msg_id: str = ""
    at_bot: bool = False
    bot_id: str | None = None
    # 平台给的被动回复窗口（官方 300s；OneBot 为 None）
    window_ttl: float | None = None

    @property
    def id(self) -> str:
        return self.msg_id

    @property
    def text(self) -> str:
        return segments_to_text(self.segments)

    def mentions_bot(self) -> bool:
        return self.at_bot or has_at_bot(self.segments, self.bot_id)

    def dedupe_key(self) -> str:
        # msg_id 在消息里唯一；没有 msg_id（极少见）就不拦
        return f"{self.channel.platform}:{self.msg_id}" if self.msg_id else ""


@dataclass(slots=True)
class NoticeEvent(Event):
    """通知类事件：入群/退群、撤回、禁言、管理员变更、戳一戳……

    三个关键字段分开是有意的，别混：
    - `actor`：**事件的当事人**（谁入群了、谁被禁言了）。权限判定用它。
    - `operator_id`：**谁执行了这次操作**（管理员踢人时不是本人）。
      和 `actor.id` 相同表示"本人操作自己"（比如自己退群）。
    - `notice_id`：跟这条通知相关的 ID。撤回事件里它就是**被撤回的消息 ID**。

    `notice_type` / `sub_type` 保留平台原值（OneBot 的 `group_recall`、
    `group_ban` + `ban`/`lift_ban`），不做二次归一化 —— 归一化会把
    「谁说的」和「平台怎么叫」都糊掉，出问题没法对日志。
    """

    notice_type: str = ""
    sub_type: str = ""
    notice_id: str = ""
    operator_id: str | None = None
    target_id: str | None = None

    @property
    def id(self) -> str:
        return self.notice_id

    @property
    def by_admin(self) -> bool:
        """这次操作是不是别人（大概率管理员）对我/他做的。"""
        return bool(self.operator_id) and self.operator_id != self.actor.id

    # 戳一戳天然可连发（同一秒连戳两下是正常操作），组合 key 会误杀，直接不参与去重
    _NO_DEDUPE = frozenset({("notify", "poke")})

    def dedupe_key(self) -> str:
        """没有统一 id 的通知怎么判重。

        - 撤回类：`notice_id` 借的是 `message_id`，真正唯一
        - 其余：只能靠「类型 + 上报时间 + 群 + 谁 + 对谁」组合。
          `time` 是**秒级**，所以只在同一秒内生效 —— 这正好覆盖「上报端
          超时重试」这个场景，又不会把同一人隔几秒的正常操作吃掉
        - 拿不到 `raw.time`（比如测试里手搓的事件）：返回空串，不拦
        """
        platform = self.channel.platform
        if self.notice_id:
            return f"{platform}:notice:{self.notice_type}:{self.notice_id}"
        if (self.notice_type, self.sub_type) in self._NO_DEDUPE:
            return ""
        ts = _raw_ts(self.raw)
        if not ts:
            return ""
        parts = (
            platform,
            "notice",
            self.notice_type,
            self.sub_type,
            ts,
            self.channel.kind.value,
            self.channel.id,
            self.actor.id,
            self.operator_id or "",
            self.target_id or "",
        )
        return ":".join(parts)


@dataclass(slots=True)
class RequestEvent(Event):
    """请求类事件：加好友、入群申请、群邀请。

    `flag` 是平台发的**审批凭据**，批准/拒绝时必须原样回传，不能自己拼。
    """

    request_type: str = ""  # friend / group
    sub_type: str = ""  # add / invite
    flag: str = ""
    comment: str = ""
    group_id: str | None = None

    @property
    def id(self) -> str:
        return self.flag

    def dedupe_key(self) -> str:
        """`flag` 是平台发的审批凭据，天然唯一 —— 重复上报只能处理一次。

        （重复审批会往群里回两次「已同意」，很难看。）没有 flag 时退回组合 key。
        """
        platform = self.channel.platform
        if self.flag:
            return f"{platform}:request:{self.flag}"
        ts = _raw_ts(self.raw)
        if not ts:
            return ""
        return ":".join(
            (platform, "request", self.request_type, self.sub_type, ts, self.channel.id, self.actor.id)
        )
