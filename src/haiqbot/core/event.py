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
