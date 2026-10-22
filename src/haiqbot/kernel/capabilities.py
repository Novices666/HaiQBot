"""内核**发放**的能力句柄：基础设施（net / storage / kv / schedule / moderation）。

这里刻意没有 image / llm ——生图与对话属于**插件领域**，
它们的服务名与方法由插件用 `ServiceSpec`声明，内核只保管能力表。

本模块的句柄只做一件事：把内核资源以受限方法暴露给插件，凭据永不外泄。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from haiqbot.core.errors import CapabilityDenied, CapabilityUnavailable
from haiqbot.kernel import request_context
from haiqbot.kernel.log import get_logger

log = get_logger(__name__)

# 只剩内核自己实现的基础设施 +适配器实现的平台动作。
# 注意：它**不再是能力白名单**（插件可以声明任意名字的服务），
# 只用于给这几个内置句柄取名字，并给 unknown告警当参照。
CAP_NET = "net:http"
CAP_STORAGE = "storage:media"
CAP_KV = "kv"
CAP_SCHEDULE = "schedule"
CAP_MODERATION = "moderation"

CAP_PLATFORM = "platform"

ALL_CAPABILITIES = frozenset({CAP_NET, CAP_STORAGE, CAP_KV, CAP_SCHEDULE, CAP_MODERATION, CAP_PLATFORM})

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class ModerationCap:
    """群管理动作：撤回 / 禁言 / 踢人 / 成员列表 / 审批。

    为什么值得单独一个能力：这些动作**改的是别人的状态**，比"发消息"危险得多。
    放在能力句柄里才有三个好处：

    1. 统一审计 —— 每次动作都落 `control`-同级的审计行，事后能查"谁在什么时候
       把谁踢了"；
    2. 能力协商 —— `ctx.moderation.supports("mute")` 先问再发文案，而不是 try/except；
    3. 拿不到适配器实例 —— 插件只能调这四个方法，不能绕过内核直接 `call()`。

    平台不支持就抛 `CapabilityUnavailable`（不是静默 False），让插件能明确降级。
    """

    ACTIONS = ("recall", "mute", "kick", "member_list", "request")

    def __init__(self, plugin_id: str, adapters: Any = None, audit: Any = None) -> None:
        self._plugin_id = plugin_id
        self._adapters = adapters if adapters is not None else {}
        self._audit = audit

    # -- 探测 ------------------------------------------------------------- #
    def _adapter(self, channel: Any) -> Any:
        adapter = self._adapters.get(getattr(channel, "platform", ""))
        if adapter is None:
            raise CapabilityUnavailable(f"平台未接入：{getattr(channel, 'platform', '?')}")
        return adapter

    def supports(self, action: str, channel: Any = None) -> bool:
        """这个平台支不支持某个动作。`channel` 省略 = 任一平台支持即 true。"""
        if action not in self.ACTIONS:
            return False
        if channel is None:
            return any(action in getattr(a, "actions", frozenset()) for a in self._adapters.values())
        adapter = self._adapters.get(getattr(channel, "platform", ""))
        return action in getattr(adapter, "actions", frozenset()) if adapter else False

    async def _audit_action(self, action: str, channel: Any, detail: str, ok: bool) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.record(
                trace_id=request_context.current_trace() or "",
                plugin_id=self._plugin_id,
                action=f"moderation:{action}",
                channel=str(channel) if channel is not None else None,
                actor=request_context.current_actor(),
                detail=detail if ok else f"{detail} -> failed",
            )
        except Exception:  # noqa: BLE001 - 审计失败不该阻断动作
            log.warning("moderation_audit_failed", action=action, plugin=self._plugin_id)

    # -- 动作 ------------------------------------------------------------- #
    async def recall(self, channel: Any, message_id: str) -> bool:
        adapter = self._adapter(channel)
        ok = await adapter.recall(channel, message_id)
        await self._audit_action("recall", channel, f"message_id={message_id}", ok)
        return ok

    async def mute(self, channel: Any, user_id: str, seconds: int) -> bool:
        adapter = self._adapter(channel)
        ok = await adapter.mute(channel, str(user_id), int(seconds))
        await self._audit_action("mute", channel, f"user={user_id} seconds={seconds}", ok)
        return ok

    async def kick(self, channel: Any, user_id: str, *, reject_add_request: bool = False) -> bool:
        adapter = self._adapter(channel)
        ok = await adapter.kick(channel, str(user_id), reject_add_request=reject_add_request)
        await self._audit_action("kick", channel, f"user={user_id} reject_add={reject_add_request}", ok)
        return ok

    async def member_list(self, channel: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        return await self._adapter(channel).member_list(channel, limit=limit)

    async def approve(self, request_event: Any, approve: bool, *, reason: str = "") -> bool:
        """批准 / 拒绝加好友或入群申请。`request_event` 是 RequestEvent。"""
        channel = getattr(request_event, "channel", None)
        adapter = self._adapter(channel)
        ok = await adapter.handle_request(request_event, approve, reason=reason)
        await self._audit_action(
            "approve" if approve else "reject",
            channel,
            f"flag={getattr(request_event, 'flag', '')} "
            f"from={getattr(getattr(request_event, 'actor', None), 'id', '')}",
            ok,
        )
        return ok


# --------------------------------------------------------------------------- #
class NetCap:
    """受限 HTTP 客户端：只能访问允许的域名。"""

    def __init__(self, allowed_hosts: list[str] | None = None, *, client: Any = None) -> None:
        self._allowed = {h.lower() for h in (allowed_hosts or [])}
        self._client = client

    def _check(self, url: str) -> None:
        if not self._allowed:
            return
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if host not in self._allowed:
            raise CapabilityDenied(f"域名不在白名单：{host or url}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        self._check(url)
        r = await self._http().get(url, **kwargs)
        r.raise_for_status()
        return r.json()

    async def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        self._check(url)
        r = await self._http().post(url, json=payload, **kwargs)
        r.raise_for_status()
        return r.json()

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
            self._client = None


# --------------------------------------------------------------------------- #
class StorageCap:
    """插件私有文件区，路径被限制在 base 之下。"""

    def __init__(self, base: Path, plugin_id: str) -> None:
        self.base = (base / plugin_id).resolve()
        self.base.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        if not _SAFE_NAME.match(name):
            raise ValueError(f"非法文件名：{name!r}")
        target = (self.base / name).resolve()
        if not target.is_relative_to(self.base):
            raise ValueError("越界路径")
        return target

    def write(self, name: str, data: bytes) -> Path:
        target = self.path(name)
        target.write_bytes(data)
        return target

    def read(self, name: str) -> bytes:
        return self.path(name).read_bytes()

    def list(self) -> list[str]:
        return sorted(p.name for p in self.base.iterdir() if p.is_file())


# --------------------------------------------------------------------------- #
class KVCap:
    """插件命名空间内的键值存储。"""

    def __init__(self, store: Any, plugin_id: str) -> None:
        self._store = store
        self._plugin_id = plugin_id

    async def get(self, key: str, default: str | None = None) -> str | None:
        value = await self._store.kv_get(self._plugin_id, key)
        return default if value is None else value

    async def set(self, key: str, value: str) -> None:
        await self._store.kv_set(self._plugin_id, key, str(value))

    async def delete(self, key: str) -> None:
        await self._store.kv_delete(self._plugin_id, key)

    async def all(self) -> dict[str, str]:
        return await self._store.kv_all(self._plugin_id)


# --------------------------------------------------------------------------- #
class ScheduleCap:
    def __init__(self, scheduler: Any, plugin_id: str) -> None:
        self._scheduler = scheduler
        self._plugin_id = plugin_id
        self._jobs: list[str] = []

    def every(self, seconds: float, fn: Any, *, name: str | None = None) -> str:
        job_name = f"{self._plugin_id}:{name or getattr(fn, '__name__', 'job')}"
        self._scheduler.add_interval(job_name, seconds, fn)
        self._jobs.append(job_name)
        return job_name

    def cancel_all(self) -> None:
        for job in self._jobs:
            self._scheduler.remove(job)
        self._jobs.clear()


# --------------------------------------------------------------------------- #
class IntrospectCap:
    """只读内省。

    能看到：插件清单、命令、**非机密的配置键名**与凭据槽位标签。
    永远看不到：任何凭据值。面板与 doctor 依赖这份数据。
    """

    def __init__(self, source: Any) -> None:
        self._source = source

    def commands(self) -> list[dict[str, Any]]:
        return self._source.commands()

    def plugins(self) -> list[dict[str, Any]]:
        return self._source.status()


# --------------------------------------------------------------------------- #
class PlatformCap:
    """平台查询与轻量动作。

    与 `moderation` 的分界：这里**不改别人的状态**（查群/查消息、改自己的群名片、
    上传群文件、给消息加表情回应），所以不强制审计；但同样要先用 `supports()`
    探测，平台不支持时抛 `CapabilityUnavailable`，让插件明确降级。

    不传 `channel` 时用当前事件的频道 —— 所以插件代码里通常不用传。
    """

    ACTIONS = (
        "get_message",
        "get_forward",
        "group_list",
        "group_info",
        "member_info",
        "stranger_info",
        "set_group_card",
        "upload_group_file",
        "emoji_like",
        "login_info",
        "status",
        "friend_list",
        "essence_list",
        "set_group_name",
        "leave_group",
        "set_group_whole_ban",
        "send_like",
        "version_info",
        "at_all_remain",
        "honor_list",
        "fetch_image",
        "fetch_record",
        "mark_read",
        "set_essence",
        "del_essence",
        "set_admin",
        "set_title",
        "set_portrait",
        "set_anonymous",
        "ban_anonymous",
        "user_status",
        "unidirectional_friends",
        "sign_info",
        "sign_in",
        "group_files",
        "folder_files",
        "file_url",
        "file_system_info",
        "create_folder",
        "delete_file",
        "delete_folder",
        "can_send_image",
        "can_send_record",
        "system_messages",
    )

    # 会改变现状的动作：必须留审计
    AUDITED = frozenset(
        {
            "set_group_card",
            "upload_group_file",
            "set_group_name",
            "leave_group",
            "set_group_whole_ban",
            "send_like",
            "set_essence",
            "del_essence",
            "set_admin",
            "set_title",
            "set_portrait",
            "set_anonymous",
            "ban_anonymous",
            "sign_in",
            "create_folder",
            "delete_file",
            "delete_folder",
        }
    )

    @property
    def audited(self) -> frozenset[str]:
        """会改现状、必留审计的动作名（文档与面板据此生成）。"""
        return self.AUDITED

    def __init__(self, plugin_id: str, adapters: Any = None, audit: Any = None) -> None:
        self._plugin_id = plugin_id
        self._adapters = adapters if adapters is not None else {}
        self._audit = audit

    # -- 解析 --------------------------------------------------------------- #
    def _channel(self, channel: Any = None) -> Any:
        if channel is not None:
            return channel
        event = request_context.current_event()
        current = getattr(event, "channel", None)
        if current is None:
            raise CapabilityUnavailable("没有当前频道：请在群里触发，或显式传 channel")
        return current

    def _adapter(self, channel: Any = None) -> Any:
        target = channel if channel is not None else request_context.current_event()
        target = getattr(target, "channel", target)
        platform = getattr(target, "platform", "") if target is not None else ""
        if platform:
            adapter = self._adapters.get(platform)
            if adapter is None:
                raise CapabilityUnavailable(f"平台未接入：{platform}")
            return adapter
        # 没有频道上下文（定时任务 / CLI / 机器人自身信息）：单平台就直接用
        if len(self._adapters) == 1:
            return next(iter(self._adapters.values()))
        if not self._adapters:
            raise CapabilityUnavailable("没有已接入的平台")
        raise CapabilityUnavailable("有多个平台在跑，请显式传 channel")

    def supports(self, action: str, channel: Any = None) -> bool:
        """这个平台支不支持某个动作。`channel` 省略 = 任一平台支持即 true。"""
        if action not in self.ACTIONS:
            return False
        if channel is None:
            return any(action in getattr(a, "actions", frozenset()) for a in self._adapters.values())
        adapter = self._adapters.get(getattr(channel, "platform", ""))
        return action in getattr(adapter, "actions", frozenset()) if adapter else False

    # -- 查询 --------------------------------------------------------------- #
    async def get_message(self, message_id: str, *, channel: Any = None) -> dict[str, Any] | None:
        return await self._adapter(channel).get_message(message_id)

    async def get_forward(self, message_id: str, *, channel: Any = None) -> list[dict[str, Any]]:
        return await self._adapter(channel).get_forward(message_id)

    async def group_list(self, *, channel: Any = None) -> list[dict[str, Any]]:
        return await self._adapter(channel).group_list()

    async def group_info(self, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).group_info(target)

    async def member_info(self, user_id: str, *, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).member_info(target, user_id)

    async def stranger_info(self, user_id: str, *, channel: Any = None) -> dict[str, Any] | None:
        return await self._adapter(channel).stranger_info(user_id)

    # -- 动作 --------------------------------------------------------------- #
    async def set_group_card(self, user_id: str, card: str, *, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).set_group_card(target, user_id, card)
        await self._record("set_group_card", f"user={user_id} len(card)={len(card)}", ok)
        return ok

    async def upload_group_file(self, path: str, name: str, *, channel: Any = None) -> str | None:
        target = self._channel(channel)
        file_id = await self._adapter(target).upload_group_file(target, path, name)
        await self._record("upload_group_file", f"name={name}", file_id is not None)
        return file_id

    async def emoji_like(self, message_id: str, code: str, *, channel: Any = None) -> bool:
        return await self._adapter(channel).set_emoji_like(message_id, code)

    async def login_info(self, *, channel: Any = None) -> dict[str, Any] | None:
        return await self._adapter(channel).login_info()

    async def status(self, *, channel: Any = None) -> dict[str, Any] | None:
        return await self._adapter(channel).status()

    async def friend_list(self, *, channel: Any = None) -> list[dict[str, Any]]:
        return await self._adapter(channel).friend_list()

    async def essence_list(self, channel: Any = None) -> list[dict[str, Any]]:
        target = self._channel(channel)
        return await self._adapter(target).essence_list(target)

    async def version_info(self, *, channel: Any = None) -> dict[str, Any] | None:
        return await self._adapter(channel).version_info()

    async def at_all_remain(self, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).at_all_remain(target)

    async def honor_list(self, kind: str = "all", *, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).honor_list(target, kind)

    async def fetch_image(self, file_id: str, *, channel: Any = None) -> dict[str, Any] | None:
        data = await self._adapter(channel).fetch_image(file_id)
        return self._media_ref(data)

    async def fetch_record(self, file_id: str, *, channel: Any = None) -> dict[str, Any] | None:
        data = await self._adapter(channel).fetch_record(file_id)
        return self._media_ref(data)

    @staticmethod
    def _media_ref(data: dict[str, Any] | None) -> dict[str, Any] | None:
        """归一化媒体返回：区分 `url` 与协议端本机 `path`。

        `get_image` / `get_record` 回的 `file` 可能是 http(s) 直链，也可能是
        **协议端所在机器**的绝对路径。容器/跨机部署时后者宿主侧读不到，
        所以显式拆成 `url` / `path` 并给 `local` 标记，由调用方决定怎么取。
        """
        if not isinstance(data, dict):
            return data
        ref = dict(data)
        raw = str(ref.get("file") or ref.get("path") or "")
        known = ref.get("url")
        url = known if isinstance(known, str) and known.startswith(("http://", "https://")) else None
        if url is None and raw.startswith(("http://", "https://")):
            url = raw
        path = None if url is not None else (raw or None)
        ref["url"] = url
        ref["path"] = path
        # local=True：只有一个协议端本机路径，容器部署要配共享卷才读得到
        ref["local"] = path is not None
        return ref

    async def mark_read(self, message_id: str, *, channel: Any = None) -> bool:
        return await self._adapter(channel).mark_read(message_id)

    async def set_essence(self, message_id: str, *, channel: Any = None) -> bool:
        ok = await self._adapter(channel).set_essence(message_id)
        await self._record("set_essence", f"message_id={message_id}", ok)
        return ok

    async def del_essence(self, message_id: str, *, channel: Any = None) -> bool:
        ok = await self._adapter(channel).del_essence(message_id)
        await self._record("del_essence", f"message_id={message_id}", ok)
        return ok

    async def set_admin(self, user_id: str, enable: bool = True, *, channel: Any = None) -> bool:
        """设/撤管理员 —— 提权动作，必留审计。"""
        target = self._channel(channel)
        ok = await self._adapter(target).set_admin(target, user_id, enable)
        await self._record("set_admin", f"user={user_id} enable={enable}", ok)
        return ok

    async def set_title(self, user_id: str, title: str, *, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).set_title(target, user_id, title)
        await self._record("set_title", f"user={user_id} title={title}", ok)
        return ok

    async def can_send_image(self, channel: Any = None) -> bool:
        """协议端当前能不能发图（不用猜，直接问）。"""
        return await self._adapter(channel).can_send_image(channel)

    async def can_send_record(self, channel: Any = None) -> bool:
        return await self._adapter(channel).can_send_record(channel)

    async def system_messages(self, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).system_messages(target)

    # -- 群管理补充 / 社交 / 群文件 ------------------------------------------ #
    async def set_portrait(self, path: str, *, channel: Any = None) -> bool:
        """改群头像（`path` 必须是协议端能读到的路径或 URL）。"""
        target = self._channel(channel)
        ok = await self._adapter(target).set_portrait(target, path)
        await self._record("set_portrait", f"group={target.id}", ok)
        return ok

    async def set_anonymous(self, enable: bool, *, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).set_anonymous(target, enable)
        await self._record("set_anonymous", f"enable={enable}", ok)
        return ok

    async def ban_anonymous(self, flag: str, seconds: int = 60, *, channel: Any = None) -> bool:
        """禁言匿名发言人（`flag` 取自事件的 `anonymous.flag`）。"""
        target = self._channel(channel)
        ok = await self._adapter(target).ban_anonymous(target, flag, seconds)
        await self._record("ban_anonymous", f"seconds={seconds}", ok)
        return ok

    async def user_status(self, user_id: str, *, channel: Any = None) -> dict[str, Any] | None:
        return await self._adapter(channel).user_status(user_id)

    async def unidirectional_friends(self, *, channel: Any = None) -> list[dict[str, Any]]:
        return await self._adapter(channel).unidirectional_friends()

    async def sign_info(self, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).sign_info(target)

    async def sign_in(self, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).sign_in(target)
        await self._record("sign_in", f"group={target.id}", ok)
        return ok

    async def group_files(self, channel: Any = None) -> dict[str, list[dict[str, Any]]]:
        """根目录：`{"files": [...], "folders": [...]}`（原始字段 + id/name）。"""
        target = self._channel(channel)
        return await self._adapter(target).group_files(target)

    async def folder_files(self, folder_id: str, *, channel: Any = None) -> dict[str, list[dict[str, Any]]]:
        target = self._channel(channel)
        return await self._adapter(target).folder_files(target, folder_id)

    async def file_url(self, file_id: str, busid: int = 0, *, channel: Any = None) -> str | None:
        """下载地址。`busid` 来自文件条目本身，别自己猜。"""
        target = self._channel(channel)
        return await self._adapter(target).file_url(target, file_id, busid)

    async def file_system_info(self, channel: Any = None) -> dict[str, Any] | None:
        target = self._channel(channel)
        return await self._adapter(target).file_system_info(target)

    async def create_folder(self, name: str, parent_id: str = "", *, channel: Any = None) -> str | None:
        target = self._channel(channel)
        folder_id = await self._adapter(target).create_folder(target, name, parent_id)
        await self._record("create_folder", f"name={name}", folder_id is not None)
        return folder_id

    async def delete_file(self, file_id: str, busid: int = 0, *, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).delete_file(target, file_id, busid)
        await self._record("delete_file", f"file_id={file_id}", ok)
        return ok

    async def delete_folder(self, folder_id: str, *, channel: Any = None) -> bool:
        """删文件夹会连里面的文件一起删 —— 不可逆，必留审计。"""
        target = self._channel(channel)
        ok = await self._adapter(target).delete_folder(target, folder_id)
        await self._record("delete_folder", f"folder_id={folder_id}", ok)
        return ok

    # -- 会改现状的动作（过闸门 + 审计） ------------------------------------ #
    async def _record(self, action: str, detail: str, ok: bool) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.record(
                trace_id=request_context.current_trace() or "",
                plugin_id=self._plugin_id,
                action=f"platform:{action}",
                channel=request_context.current_channel(),
                actor=request_context.current_actor(),
                detail=detail if ok else f"{detail} -> failed",
            )
        except Exception:  # noqa: BLE001 - 审计失败不阻断动作
            log.warning("platform_audit_failed", action=action, plugin=self._plugin_id)

    async def set_group_name(self, name: str, *, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).set_group_name(target, name)
        await self._record("set_group_name", f"name={name}", ok)
        return ok

    async def leave_group(self, *, channel: Any = None) -> bool:
        """让机器人退群 —— 危险动作，必留审计。"""
        target = self._channel(channel)
        ok = await self._adapter(target).leave_group(target)
        await self._record("leave_group", f"group={target.id}", ok)
        return ok

    async def set_group_whole_ban(self, enable: bool, *, channel: Any = None) -> bool:
        target = self._channel(channel)
        ok = await self._adapter(target).set_group_whole_ban(target, enable)
        await self._record("set_group_whole_ban", f"enable={enable}", ok)
        return ok

    async def send_like(self, user_id: str, times: int = 1, *, channel: Any = None) -> bool:
        ok = await self._adapter(channel).send_like(user_id, times)
        await self._record("send_like", f"user={user_id} times={times}", ok)
        return ok


def build_capabilities(
    manifest: Any,
    *,
    get_service: Callable[[str], Any] | None = None,
    store: Any = None,
    scheduler: Any = None,
    storage_root: Path | None = None,
    net_hosts: list[str] | None = None,
    quota: Any = None,
    adapters: Any = None,
    audit: Any = None,
) -> dict[str, Any]:
    """按 manifest声明构造**基础设施**句柄表。未声明的一律 None。

    只处理内核自己的资源（net / storage / kv / schedule / moderation）。
    图像、对话这类插件领域的服务**不在这里**：它们由插件用 `provides` +
    `ServiceSpec`注册进能力表，消费方用 `ctx.service()`取。
    """
    declared = set(manifest.capabilities)
    non_kernel = declared - ALL_CAPABILITIES
    if non_kernel:
        # 不是错误：插件可以声明任意领域服务（走的是 provides那条路）。
        # 这里只提示「你可能把本该写进 services的东西写进了 capabilities」。
        log.debug("non_kernel_capabilities", plugin=manifest.id, names=sorted(non_kernel))

    caps: dict[str, Any] = {
        "net": None,
        "storage": None,
        "kv": None,
        "schedule": None,
        "moderation": None,
        "platform": None,
    }
    if CAP_NET in declared:
        caps["net"] = NetCap(net_hosts)
    if CAP_STORAGE in declared and storage_root is not None:
        caps["storage"] = StorageCap(storage_root, manifest.id)
    if CAP_KV in declared and store is not None:
        caps["kv"] = KVCap(store, manifest.id)
    if CAP_SCHEDULE in declared and scheduler is not None:
        caps["schedule"] = ScheduleCap(scheduler, manifest.id)
    if CAP_MODERATION in declared and adapters is not None:
        caps["moderation"] = ModerationCap(manifest.id, adapters, audit)
    if CAP_PLATFORM in declared and adapters is not None:
        caps["platform"] = PlatformCap(manifest.id, adapters, audit)

    missing = [
        cap
        for cap, key in (
            (CAP_STORAGE, "storage"),
            (CAP_KV, "kv"),
            (CAP_SCHEDULE, "schedule"),
            (CAP_MODERATION, "moderation"),
            (CAP_PLATFORM, "platform"),
        )
        if cap in declared and caps[key] is None
    ]
    if missing:
        log.warning("capability_unavailable", plugin=manifest.id, missing=missing)
    return caps
