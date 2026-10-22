"""适配器抽象。

适配器只做两件事：把平台事件**归一化**成 `Event`，把 `Segment` 列表**编码**成平台消息。
任何平台特有的东西（msg_seq、upload、窗口时长）都不许泄漏到内核以上。
"""

from __future__ import annotations

import abc
import inspect
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from haiqbot.core.channel import ChannelId
from haiqbot.core.errors import AdapterError
from haiqbot.core.event import Event
from haiqbot.core.reply import PlatformCapabilities
from haiqbot.core.segment import Segment

if TYPE_CHECKING:
    pass

PushFn = Callable[[Event], Awaitable[None]]


class Adapter(abc.ABC):
    """所有平台适配器的基类。"""

    platform: str = "unknown"
    capabilities: PlatformCapabilities = PlatformCapabilities()
    bot_id: str | None = None
    # 出站闸门，由 App 装配时注入（None = 不设限，仅测试用）
    outbound: Any = None

    def __init__(self, push: PushFn | None = None) -> None:
        self._push = push
        # 最近一次收到平台事件的时间戳（面板的「连接健康度」用它判断"还活着吗"）
        self._last_event_ts: float | None = None
        self._started_at: float | None = None

    async def _guarded_send(
        self,
        channel: ChannelId,
        fn: Callable[[], Awaitable[Any]],
        *,
        plugin_id: str | None = None,
        retryable: Callable[[BaseException], bool] | None = None,
    ) -> Any:
        """把一次真实发送交给出站闸门（同频道串行 + 间隔 + 全局限速 + 退避重试）。"""
        if self.outbound is None:
            return await fn()
        key = f"{self.platform}:{channel.kind}:{channel.id}"
        return await self.outbound.send(key, fn, retryable=retryable, plugin_id=plugin_id)

    def bind(self, push: PushFn) -> None:
        self._push = push

    async def _emit(self, event: Event) -> None:
        if self._push is None:
            raise RuntimeError(f"{type(self).__name__} 尚未绑定事件推送函数")
        self._last_event_ts = time.time()
        # 契约是 async，但同步回调（测试/一次性脚本常这么写）不该在事件到达时
        # 才炸出 "object NoneType can't be used in 'await' expression"
        result = self._push(event)
        if inspect.isawaitable(result):
            await result

    def mark_started(self) -> None:
        self._started_at = time.time()

    # -- 健康度 ----------------------------------------------------------- #
    def health(self) -> dict[str, Any]:
        """连接健康度。子类覆盖以补充平台特有信号。

        `connected` 为 None 表示该平台没有可靠的连接态可观测（别硬编成 True）。
        """
        return {
            "connected": None,
            "last_event_ts": self._last_event_ts,
            "started_at": self._started_at,
        }

    # -- 生命周期 --------------------------------------------------------- #
    @abc.abstractmethod
    async def start(self) -> None:
        """建立连接 / 启动后台任务。"""

    @abc.abstractmethod
    async def stop(self) -> None:
        """释放资源。"""

    # -- 管理动作（可选，平台不支持就抛 AdapterError） ---------------------- #
    # 为什么放在适配器而不是内核：撒回 / 禁言 / 踢人是**平台动作**，
    # 参数语义（秒数上限、是否需要群主）各平台不同。内核只负责把动作
    # 转成「一次被审计、被限速的调用」，不认识具体平台。
    #
    # `actions` 是能力协商用的：插件可以先问 `ctx.moderation.supports("mute")`，
    # 再决定回什么文案 —— 而不是 try/except 猜。
    actions: frozenset[str] = frozenset()

    async def recall(self, channel: ChannelId, message_id: str) -> bool:
        raise AdapterError(f"{self.platform} 不支持撤回消息")

    async def mute(self, channel: ChannelId, user_id: str, seconds: int) -> bool:
        """禁言；`seconds=0` 表示解除。"""
        raise AdapterError(f"{self.platform} 不支持禁言")

    async def kick(self, channel: ChannelId, user_id: str, *, reject_add_request: bool = False) -> bool:
        raise AdapterError(f"{self.platform} 不支持踢人")

    async def member_list(self, channel: ChannelId, *, limit: int = 100) -> list[dict[str, Any]]:
        raise AdapterError(f"{self.platform} 不支持读取成员列表")

    # -- 平台查询与轻量动作（默认不支持，由具体 adapter 覆盖） -------------- #
    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        return None

    async def get_forward(self, message_id: str) -> list[dict[str, Any]]:
        return []

    async def group_list(self) -> list[dict[str, Any]]:
        return []

    async def group_info(self, channel: ChannelId) -> dict[str, Any] | None:
        return None

    async def member_info(self, channel: ChannelId, user_id: str) -> dict[str, Any] | None:
        return None

    async def stranger_info(self, user_id: str) -> dict[str, Any] | None:
        return None

    async def login_info(self) -> dict[str, Any] | None:
        return None

    async def status(self) -> dict[str, Any] | None:
        return None

    async def friend_list(self) -> list[dict[str, Any]]:
        return []

    async def essence_list(self, channel: ChannelId) -> list[dict[str, Any]]:
        return []

    async def set_group_name(self, channel: ChannelId, name: str) -> bool:
        raise AdapterError(f"{self.name} 不支持修改群名")

    async def leave_group(self, channel: ChannelId) -> bool:
        raise AdapterError(f"{self.name} 不支持主动退群")

    async def set_group_whole_ban(self, channel: ChannelId, enable: bool) -> bool:
        raise AdapterError(f"{self.name} 不支持全员禁言")

    async def version_info(self) -> dict[str, Any] | None:
        return None

    async def at_all_remain(self, channel: ChannelId) -> dict[str, Any] | None:
        return None

    async def honor_list(self, channel: ChannelId, kind: str = "all") -> dict[str, Any] | None:
        return None

    async def fetch_image(self, file_id: str) -> dict[str, Any] | None:
        return None

    async def fetch_record(self, file_id: str) -> dict[str, Any] | None:
        return None

    async def mark_read(self, message_id: str) -> bool:
        raise AdapterError(f"{self.name} 不支持已读回执")

    async def set_essence(self, message_id: str) -> bool:
        raise AdapterError(f"{self.name} 不支持设置精华")

    async def del_essence(self, message_id: str) -> bool:
        raise AdapterError(f"{self.name} 不支持移除精华")

    async def set_admin(self, channel: ChannelId, user_id: str, enable: bool) -> bool:
        raise AdapterError(f"{self.name} 不支持设置管理员")

    async def set_title(self, channel: ChannelId, user_id: str, title: str) -> bool:
        raise AdapterError(f"{self.name} 不支持设置群头衔")

    async def set_portrait(self, channel: ChannelId, path: str) -> bool:
        raise AdapterError(f"{self.name} 不支持改群头像")

    async def set_anonymous(self, channel: ChannelId, enable: bool) -> bool:
        raise AdapterError(f"{self.name} 不支持开/关匿名")

    async def ban_anonymous(self, channel: ChannelId, flag: str, seconds: int = 60) -> bool:
        raise AdapterError(f"{self.name} 不支持禁言匿名成员")

    async def user_status(self, user_id: str) -> dict[str, Any] | None:
        raise AdapterError(f"{self.name} 不支持查用户状态")

    async def unidirectional_friends(self) -> list[dict[str, Any]]:
        raise AdapterError(f"{self.name} 不支持查单向好友")

    async def sign_info(self, channel: ChannelId) -> dict[str, Any] | None:
        raise AdapterError(f"{self.name} 不支持查群签到")

    async def sign_in(self, channel: ChannelId) -> bool:
        raise AdapterError(f"{self.name} 不支持群签到")

    async def can_send_image(self, channel: ChannelId | None = None) -> bool:
        raise AdapterError(f"{self.name} 不支持探测能否发图")

    async def can_send_record(self, channel: ChannelId | None = None) -> bool:
        raise AdapterError(f"{self.name} 不支持探测能否发语音")

    async def system_messages(self, channel: ChannelId) -> dict[str, Any] | None:
        raise AdapterError(f"{self.name} 不支持查群系统消息")

    async def group_files(self, channel: ChannelId) -> dict[str, list[dict[str, Any]]]:
        raise AdapterError(f"{self.name} 不支持查群文件")

    async def folder_files(self, channel: ChannelId, folder_id: str) -> dict[str, list[dict[str, Any]]]:
        raise AdapterError(f"{self.name} 不支持查群文件夹")

    async def file_url(self, channel: ChannelId, file_id: str, busid: int) -> str | None:
        raise AdapterError(f"{self.name} 不支持取群文件下载地址")

    async def file_system_info(self, channel: ChannelId) -> dict[str, Any] | None:
        raise AdapterError(f"{self.name} 不支持查群文件空间")

    async def create_folder(self, channel: ChannelId, name: str, parent_id: str = "") -> str | None:
        raise AdapterError(f"{self.name} 不支持建群文件夹")

    async def delete_file(self, channel: ChannelId, file_id: str, busid: int) -> bool:
        raise AdapterError(f"{self.name} 不支持删群文件")

    async def delete_folder(self, channel: ChannelId, folder_id: str) -> bool:
        raise AdapterError(f"{self.name} 不支持删群文件夹")

    async def send_like(self, user_id: str, times: int = 1) -> bool:
        raise AdapterError(f"{self.name} 不支持点赞")

    async def set_group_card(self, channel: ChannelId, user_id: str, card: str) -> bool:
        raise AdapterError(f"{self.name} 不支持设置群名片")

    async def upload_group_file(self, channel: ChannelId, path: str, name: str) -> str | None:
        raise AdapterError(f"{self.name} 不支持上传群文件")

    async def set_emoji_like(self, message_id: str, code: str) -> bool:
        raise AdapterError(f"{self.name} 不支持表情回应")

    async def handle_request(self, event: Any, approve: bool, *, reason: str = "") -> bool:
        """批准 / 拒绝加好友或入群申请。`event` 必须是 RequestEvent。"""
        raise AdapterError(f"{self.platform} 不支持处理请求")

    async def send_forward(
        self, channel: ChannelId, nodes: list[dict[str, Any]], *, plugin_id: str | None = None
    ) -> str | None:
        """合并转发。`nodes` 是 `ForwardNode.to_dict()` 的结果。"""
        raise AdapterError(f"{self.platform} 不支持合并转发")

    # -- 发送 ------------------------------------------------------------- #
    @abc.abstractmethod
    async def send_message(
        self,
        channel: ChannelId,
        segments: list[Segment],
        *,
        reply_to: str | None = None,
        seq: int | None = None,
        plugin_id: str | None = None,
    ) -> str | None:
        """发送消息，返回平台消息 ID（拿不到就返回 None）。"""

    async def send_text(self, channel: ChannelId, text: str, **kwargs: object) -> str | None:
        return await self.send_message(channel, [Segment.text(text)], **kwargs)  # type: ignore[arg-type]

    # -- 自检 ------------------------------------------------------------- #
    def describe(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "bot_id": self.bot_id,
            "capabilities": {
                "image": self.capabilities.image,
                "markdown": self.capabilities.markdown,
                "button": self.capabilities.button,
                "file": self.capabilities.file,
                "window_ttl": self.capabilities.window_ttl,
            },
            "health": self.health(),
            # 面板据此显示「这个平台能做什么」，插件据此决定降级文案
            "actions": sorted(self.actions),
        }
