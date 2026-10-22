"""core 层：与具体 QQ 协议无关的消息/事件/会话/回复抽象。"""

from .channel import Actor, ChannelId, ChannelKind, Role
from .errors import CapabilityDenied, HaiqError, HandlerTimeout, StopPipeline
from .event import Event, EventKind, MessageEvent, NoticeEvent
from .reply import ImageRef, PlatformCapabilities, Reply
from .segment import Segment, SegmentKind, segments_to_text

__all__ = [
    "Actor",
    "CapabilityDenied",
    "ChannelId",
    "ChannelKind",
    "Event",
    "EventKind",
    "HandlerTimeout",
    "ImageRef",
    "MessageEvent",
    "NoticeEvent",
    "PlatformCapabilities",
    "Reply",
    "Role",
    "Segment",
    "SegmentKind",
    "StopPipeline",
    "HaiqError",
    "segments_to_text",
]
