"""插件作者唯一需要 import 的模块。



    from haiqbot import Plugin, Context, SecretSlot, on_command



刻意保持极小的导出面：插件不该需要四处 import 内核内部模块。

"""

from __future__ import annotations

from haiqbot.core.channel import ChannelId, ChannelKind, Role
from haiqbot.core.event import Event, EventKind, MessageEvent, NoticeEvent, RequestEvent
from haiqbot.core.reply import ImageRef, PlatformCapabilities, Reply
from haiqbot.core.segment import Segment, SegmentKind
from haiqbot.kernel.log import get_logger
from haiqbot.kernel.plugin import (
    PLUGIN_API_VERSION,
    Context,
    Plugin,
    PluginManifest,
    ProviderContext,
    SecretHandle,
    SecretSlot,
    SecretValue,
    ServiceSpec,
    on_command,
    on_message,
    on_meta,
    on_notice,
    on_regex,
    on_request,
    resolve_secret_ref,
)
from haiqbot.services.media_store import MediaStore
from haiqbot.services.providers.base import ImageProvider, ProviderError

__all__ = [
    "PLUGIN_API_VERSION",
    "ChannelId",
    "ChannelKind",
    "Context",
    "Event",
    "EventKind",
    "ImageRef",
    "MessageEvent",
    "NoticeEvent",
    "RequestEvent",
    "PlatformCapabilities",
    "Plugin",
    "PluginManifest",
    "ProviderContext",
    "get_logger",
    "MediaStore",
    "ImageProvider",
    "ProviderError",
    "Reply",
    "SecretHandle",
    "SecretSlot",
    "SecretValue",
    "ServiceSpec",
    "Role",
    "Segment",
    "SegmentKind",
    "on_command",
    "on_message",
    "on_meta",
    "on_notice",
    "on_request",
    "on_regex",
    "resolve_secret_ref",
]
