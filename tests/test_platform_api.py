"""平台动作面：PlatformCap 路由 + OneBot action 参数映射。"""

from __future__ import annotations

from typing import Any

import pytest

from haiqbot.adapters.base import Adapter
from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.errors import AdapterError, CapabilityUnavailable
from haiqbot.kernel.capabilities import PlatformCap

GROUP = ChannelId("onebot", ChannelKind.GROUP, "100")


class FakeAdapter(Adapter):
    name = "fake"
    actions = frozenset(
        {
            "get_message",
            "group_list",
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
        }
    )

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, Any]] = []
        # fetch_image / fetch_record 的返回值，测试里按需改
        self.media_result: dict[str, Any] | None = None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_message(self, channel: Any, segments: Any, **kwargs: Any) -> str | None:
        return None

    async def send_forward(self, channel: Any, nodes: Any, **kwargs: Any) -> str | None:
        return None

    async def recall(self, channel: Any, message_id: str) -> bool:
        return True

    async def mute(self, channel: Any, user_id: str, seconds: int) -> bool:
        return True

    async def kick(self, channel: Any, user_id: str, **kwargs: Any) -> bool:
        return True

    async def member_list(self, channel: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    async def handle_request(self, event: Any, approve: bool, **kwargs: Any) -> bool:
        return True

    async def get_message(self, message_id: str) -> dict[str, Any] | None:
        self.seen.append(("get_message", message_id))
        return {"message_id": message_id}

    async def group_list(self) -> list[dict[str, Any]]:
        self.seen.append(("group_list", None))
        return [{"group_id": "100"}]

    async def set_emoji_like(self, message_id: str, code: str) -> bool:
        self.seen.append(("emoji_like", (message_id, code)))
        return True

    async def login_info(self) -> dict[str, Any] | None:
        self.seen.append(("login_info", None))
        return {"user_id": 42, "nickname": "bot"}

    async def status(self) -> dict[str, Any] | None:
        self.seen.append(("status", None))
        return {"online": True}

    async def friend_list(self) -> list[dict[str, Any]]:
        self.seen.append(("friend_list", None))
        return [{"user_id": 1}]

    async def essence_list(self, channel: Any) -> list[dict[str, Any]]:
        self.seen.append(("essence_list", channel.id))
        return [{"message_id": 5}]

    async def set_group_name(self, channel: Any, name: str) -> bool:
        self.seen.append(("set_group_name", (channel.id, name)))
        return True

    async def leave_group(self, channel: Any) -> bool:
        self.seen.append(("leave_group", channel.id))
        return True

    async def set_group_whole_ban(self, channel: Any, enable: bool) -> bool:
        self.seen.append(("set_group_whole_ban", (channel.id, enable)))
        return True

    async def send_like(self, user_id: str, times: int = 1) -> bool:
        self.seen.append(("send_like", (user_id, times)))
        return True

    async def version_info(self) -> dict[str, Any] | None:
        self.seen.append(("version_info", None))
        return {"app_name": "NapCat", "app_version": "4.2.0"}

    async def at_all_remain(self, channel: Any) -> dict[str, Any] | None:
        self.seen.append(("at_all_remain", channel.id))
        return {"can_at_all": True, "remain_at_all_count_for_group": 19}

    async def honor_list(self, channel: Any, kind: str = "all") -> dict[str, Any] | None:
        self.seen.append(("honor_list", (channel.id, kind)))
        return {"group_id": channel.id, "type": kind}

    async def fetch_image(self, file_id: str) -> dict[str, Any] | None:
        self.seen.append(("fetch_image", file_id))
        return self.media_result if self.media_result is not None else {"file": file_id}

    async def fetch_record(self, file_id: str) -> dict[str, Any] | None:
        self.seen.append(("fetch_record", file_id))
        return self.media_result if self.media_result is not None else {"file": file_id}

    async def mark_read(self, message_id: str) -> bool:
        self.seen.append(("mark_read", message_id))
        return True

    async def set_essence(self, message_id: str) -> bool:
        self.seen.append(("set_essence", message_id))
        return True

    async def del_essence(self, message_id: str) -> bool:
        self.seen.append(("del_essence", message_id))
        return True

    async def set_admin(self, channel: Any, user_id: str, enable: bool) -> bool:
        self.seen.append(("set_admin", (channel.id, user_id, enable)))
        return True

    async def set_title(self, channel: Any, user_id: str, title: str) -> bool:
        self.seen.append(("set_title", (channel.id, user_id, title)))
        return True

    async def set_portrait(self, channel: Any, path: str) -> bool:
        self.seen.append(("set_portrait", (channel.id, path)))
        return True

    async def set_anonymous(self, channel: Any, enable: bool) -> bool:
        self.seen.append(("set_anonymous", (channel.id, enable)))
        return True

    async def ban_anonymous(self, channel: Any, flag: str, seconds: int = 60) -> bool:
        self.seen.append(("ban_anonymous", (channel.id, flag, seconds)))
        return True

    async def user_status(self, user_id: str) -> dict[str, Any] | None:
        self.seen.append(("user_status", user_id))
        return {"status": 10, "ext_status": 0}

    async def unidirectional_friends(self) -> list[dict[str, Any]]:
        self.seen.append(("unidirectional_friends", None))
        return [{"user_id": 7, "nickname": "单向"}]

    async def sign_info(self, channel: Any) -> dict[str, Any] | None:
        self.seen.append(("sign_info", channel.id))
        return {"group_id": channel.id, "sign_count": 3}

    async def sign_in(self, channel: Any) -> bool:
        self.seen.append(("sign_in", channel.id))
        return True

    async def group_files(self, channel: Any) -> dict[str, list[dict[str, Any]]]:
        self.seen.append(("group_files", channel.id))
        return {
            "files": [{"id": "f1", "name": "a.txt", "size": 3}],
            "folders": [{"id": "d1", "name": "子目录", "count": 0}],
        }

    async def folder_files(self, channel: Any, folder_id: str) -> dict[str, list[dict[str, Any]]]:
        self.seen.append(("folder_files", (channel.id, folder_id)))
        return {"files": [], "folders": []}

    async def file_url(self, channel: Any, file_id: str, busid: int) -> str | None:
        self.seen.append(("file_url", (channel.id, file_id, busid)))
        return f"https://example.invalid/{file_id}?busid={busid}"

    async def file_system_info(self, channel: Any) -> dict[str, Any] | None:
        self.seen.append(("file_system_info", channel.id))
        return {"file_count": 1, "limit_count": 10000}

    async def create_folder(self, channel: Any, name: str, parent_id: str = "") -> str | None:
        self.seen.append(("create_folder", (channel.id, name, parent_id)))
        return "d-new"

    async def delete_file(self, channel: Any, file_id: str, busid: int) -> bool:
        self.seen.append(("delete_file", (channel.id, file_id, busid)))
        return True

    async def delete_folder(self, channel: Any, folder_id: str) -> bool:
        self.seen.append(("delete_folder", (channel.id, folder_id)))
        return True

    async def can_send_image(self, channel: Any = None) -> bool:
        self.seen.append(("can_send_image", channel.id if channel else None))
        return True

    async def can_send_record(self, channel: Any = None) -> bool:
        self.seen.append(("can_send_record", channel.id if channel else None))
        return False

    async def system_messages(self, channel: Any) -> dict[str, Any] | None:
        self.seen.append(("system_messages", channel.id))
        return {"invited_requests": [], "join_requests": []}


def _cap(adapter: Adapter) -> PlatformCap:
    return PlatformCap("test", adapters={"onebot": adapter})


def test_supports_reflects_adapter_actions() -> None:
    cap = _cap(FakeAdapter())
    assert cap.supports("get_message", GROUP) is True
    assert cap.supports("group_list") is True
    assert cap.supports("upload_group_file", GROUP) is False
    assert cap.supports("do_everything") is False


def test_supports_false_for_unknown_platform() -> None:
    cap = _cap(FakeAdapter())
    other = ChannelId("official", ChannelKind.GROUP, "1")
    assert cap.supports("group_list", other) is False


async def test_query_routes_to_adapter() -> None:
    adapter = FakeAdapter()
    cap = _cap(adapter)
    assert await cap.get_message("m1", channel=GROUP) == {"message_id": "m1"}
    assert await cap.group_list(channel=GROUP) == [{"group_id": "100"}]
    assert await cap.emoji_like("m1", "128077", channel=GROUP) is True
    assert adapter.seen == [
        ("get_message", "m1"),
        ("group_list", None),
        ("emoji_like", ("m1", "128077")),
    ]


async def test_single_platform_falls_back_without_event() -> None:
    """没有事件上下文（定时任务 / CLI）时，单平台直接用 —— 否则定时任务没法查数据。"""
    cap = _cap(FakeAdapter())
    assert await cap.get_message("m1") == {"message_id": "m1"}


async def test_missing_channel_raises_with_multiple_platforms() -> None:
    cap = PlatformCap("test", adapters={"onebot": FakeAdapter(), "official": FakeAdapter()})
    with pytest.raises(CapabilityUnavailable):
        await cap.get_message("m1")


async def test_unknown_platform_raises() -> None:
    cap = _cap(FakeAdapter())
    other = ChannelId("official", ChannelKind.GROUP, "1")
    with pytest.raises(CapabilityUnavailable):
        await cap.group_list(channel=other)


# --------------------------------------------------------------------------- #
# OneBot 参数映射：验证发出去的 action 名与字段
# --------------------------------------------------------------------------- #
def _onebot_with_recorder() -> tuple[OneBotAdapter, list[tuple[str, dict]]]:
    adapter = OneBotAdapter(OneBotCfg(), token_provider=lambda: None)
    calls: list[tuple[str, dict]] = []

    async def fake_call(action: str, params: dict, *, timeout: float | None = None) -> dict:
        calls.append((action, params))
        return {"status": "ok", "data": {}}

    adapter.call = fake_call  # type: ignore[method-assign]
    return adapter, calls


async def test_onebot_query_action_names() -> None:
    adapter, calls = _onebot_with_recorder()

    await adapter.get_message("m1")
    await adapter.group_list()
    await adapter.group_info(GROUP)
    await adapter.member_info(GROUP, "u1")
    await adapter.stranger_info("u1")
    await adapter.get_forward("m2")

    assert [c[0] for c in calls] == [
        "get_msg",
        "get_group_list",
        "get_group_info",
        "get_group_member_info",
        "get_stranger_info",
        "get_forward_msg",
    ]
    # OneBot 的数字 ID 会被规范化成 int（_as_id）
    assert calls[3][1] == {"group_id": 100, "user_id": "u1", "no_cache": False}
    assert calls[4][1] == {"user_id": "u1"}


async def test_onebot_emoji_like() -> None:
    adapter, calls = _onebot_with_recorder()
    assert await adapter.set_emoji_like("m1", "128077") is True
    assert calls == [("set_msg_emoji_like", {"message_id": "m1", "emoji_id": "128077"})]


async def test_onebot_rejects_card_on_private_chat() -> None:
    adapter, _ = _onebot_with_recorder()
    private = ChannelId("onebot", ChannelKind.PRIVATE, "u1")
    with pytest.raises(AdapterError):
        await adapter.set_group_card(private, "u1", "名片")


async def test_onebot_actions_advertised() -> None:
    adapter, _ = _onebot_with_recorder()
    for action in (
        "get_message",
        "get_forward",
        "group_list",
        "group_info",
        "member_info",
        "stranger_info",
        "set_group_card",
        "upload_group_file",
        "emoji_like",
    ):
        assert action in adapter.actions, action


# --------------------------------------------------------------------------- #
# 能力注册
# --------------------------------------------------------------------------- #
def test_platform_is_in_all_capabilities() -> None:
    from haiqbot.kernel.capabilities import ALL_CAPABILITIES

    assert "platform" in ALL_CAPABILITIES


def test_build_capabilities_grants_platform() -> None:
    from haiqbot.kernel.capabilities import build_capabilities

    class Manifest:
        id = "demo"
        capabilities = ("platform",)

    caps = build_capabilities(Manifest(), adapters={"onebot": FakeAdapter()})
    assert isinstance(caps["platform"], PlatformCap)
    assert caps["platform"].supports("group_list") is True


def test_build_capabilities_without_adapters() -> None:
    from haiqbot.kernel.capabilities import build_capabilities

    class Manifest:
        id = "demo"
        capabilities = ("platform",)

    caps = build_capabilities(Manifest())
    assert caps["platform"] is None


# --------------------------------------------------------------------------- #
# 新增的 8 个动作
# --------------------------------------------------------------------------- #
class _Audit:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record(self, **kwargs) -> None:
        self.calls.append(kwargs)


async def test_new_query_actions_route() -> None:
    adapter = FakeAdapter()
    cap = PlatformCap("test", adapters={"onebot": adapter})
    assert await cap.login_info() == {"user_id": 42, "nickname": "bot"}
    assert await cap.status() == {"online": True}
    assert await cap.friend_list() == [{"user_id": 1}]
    assert await cap.essence_list(channel=GROUP) == [{"message_id": 5}]


async def test_write_actions_are_audited() -> None:
    adapter = FakeAdapter()
    audit = _Audit()
    cap = PlatformCap("test", adapters={"onebot": adapter}, audit=audit)

    assert await cap.set_group_name("新群名", channel=GROUP) is True
    assert await cap.leave_group(channel=GROUP) is True
    assert await cap.set_group_whole_ban(True, channel=GROUP) is True
    assert await cap.send_like("u1", 3) is True

    actions = [call["action"] for call in audit.calls]
    assert actions == [
        "platform:set_group_name",
        "platform:leave_group",
        "platform:set_group_whole_ban",
        "platform:send_like",
    ]
    # 审计里不能出现凭据或完整 payload
    for call in audit.calls:
        assert isinstance(call["detail"], str)


async def test_supports_covers_new_actions() -> None:
    cap = PlatformCap("test", adapters={"onebot": FakeAdapter()})
    for action in (
        "login_info",
        "status",
        "friend_list",
        "essence_list",
        "set_group_name",
        "leave_group",
        "set_group_whole_ban",
        "send_like",
    ):
        assert cap.supports(action, GROUP) is True, action


# --------------------------------------------------------------------------- #
# 动作 3 批：诊断 / 荣誉 / 媒体下载 / 已读 / 精华 / 提权 / 头衔
# --------------------------------------------------------------------------- #
async def test_batch3_query_actions_route() -> None:
    adapter = FakeAdapter()
    cap = _cap(adapter)
    assert await cap.version_info() == {"app_name": "NapCat", "app_version": "4.2.0"}
    assert await cap.at_all_remain(channel=GROUP) == {
        "can_at_all": True,
        "remain_at_all_count_for_group": 19,
    }
    assert await cap.honor_list("legend", channel=GROUP) == {"group_id": "100", "type": "legend"}
    assert await cap.mark_read("m9") is True
    assert adapter.seen == [
        ("version_info", None),
        ("at_all_remain", "100"),
        ("honor_list", ("100", "legend")),
        ("mark_read", "m9"),
    ]


async def test_fetch_image_normalizes_remote_url() -> None:
    """协议端给的是直链 → url 有值、local 为 False。"""
    adapter = FakeAdapter()
    adapter.media_result = {"file": "https://gchat.qpic.cn/a.png", "file_size": 123}
    cap = _cap(adapter)
    ref = await cap.fetch_image("f1", channel=GROUP)
    assert ref is not None
    assert ref["url"] == "https://gchat.qpic.cn/a.png"
    assert ref["path"] is None
    assert ref["local"] is False
    assert ref["file_size"] == 123  # 原始字段不丢


async def test_fetch_image_marks_protocol_side_path() -> None:
    """协议端给的是本机绝对路径 → path 有值、local 为 True（容器部署读不到）。"""
    adapter = FakeAdapter()
    adapter.media_result = {"file": "C:/napcat/data/image/a.png"}
    cap = _cap(adapter)
    ref = await cap.fetch_image("f1", channel=GROUP)
    assert ref is not None
    assert ref["url"] is None
    assert ref["path"] == "C:/napcat/data/image/a.png"
    assert ref["local"] is True


async def test_fetch_record_normalizes() -> None:
    adapter = FakeAdapter()
    adapter.media_result = {"file": "https://example.invalid/a.mp3"}
    cap = _cap(adapter)
    ref = await cap.fetch_record("r1", channel=GROUP)
    assert ref is not None and ref["url"].endswith("a.mp3") and ref["local"] is False
    # 非 dict 返回原样透传（协议端可能给 None）
    assert PlatformCap._media_ref(None) is None
    assert PlatformCap._media_ref({"file": "r2"})["local"] is True


async def test_batch3_write_actions_are_audited() -> None:
    adapter = FakeAdapter()
    audit = _Audit()
    cap = PlatformCap("test", adapters={"onebot": adapter}, audit=audit)

    assert await cap.set_essence("m1", channel=GROUP) is True
    assert await cap.del_essence("m1", channel=GROUP) is True
    assert await cap.set_admin("u1", True, channel=GROUP) is True
    assert await cap.set_title("u1", "龙王", channel=GROUP) is True

    assert [call["action"] for call in audit.calls] == [
        "platform:set_essence",
        "platform:del_essence",
        "platform:set_admin",
        "platform:set_title",
    ]
    # 审计里只放标识，不放完整 payload
    for call in audit.calls:
        assert isinstance(call["detail"], str)


def test_audited_set_matches_recorded_calls() -> None:
    """AUDITED 不能是摆设：声明要审计的动作 == 真正调用 _record 的动作。"""
    import re
    from pathlib import Path

    import haiqbot.kernel.capabilities as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    declared = set(PlatformCap.AUDITED)
    recorded = set(re.findall(r'_record\(\s*"([a-z_]+)"', src))
    assert declared == recorded, {
        "declared_not_recorded": sorted(declared - recorded),
        "recorded_not_declared": sorted(recorded - declared),
    }


def test_audited_is_subset_of_actions() -> None:
    cap = _cap(FakeAdapter())
    assert cap.audited == PlatformCap.AUDITED
    assert set(PlatformCap.AUDITED) <= set(PlatformCap.ACTIONS)


async def test_onebot_batch3_action_names() -> None:
    adapter, calls = _onebot_with_recorder()

    await adapter.version_info()
    await adapter.at_all_remain(GROUP)
    await adapter.honor_list(GROUP, "strong_newbie")
    await adapter.fetch_image("file-1")
    await adapter.fetch_record("file-2")
    await adapter.mark_read("m1")
    await adapter.set_essence("m2")
    await adapter.del_essence("m2")
    await adapter.set_admin(GROUP, "u1", True)
    await adapter.set_title(GROUP, "u1", "龙王")

    assert [c[0] for c in calls] == [
        "get_version_info",
        "get_group_at_all_remain",
        "get_group_honor_info",
        "get_image",
        "get_record",
        "mark_msg_as_read",
        "set_essence_msg",
        "delete_essence_msg",
        "set_group_admin",
        "set_group_special_title",
    ]
    assert calls[1][1] == {"group_id": 100}
    assert calls[2][1] == {"group_id": 100, "type": "strong_newbie"}
    assert calls[4][1] == {"file": "file-2", "out_format": "mp3"}
    assert calls[8][1] == {"group_id": 100, "user_id": "u1", "enable": True}
    assert calls[9][1] == {"group_id": 100, "user_id": "u1", "special_title": "龙王"}


async def test_onebot_honor_list_rejects_unknown_kind() -> None:
    adapter, calls = _onebot_with_recorder()
    with pytest.raises(AdapterError):
        await adapter.honor_list(GROUP, "bogus")
    assert calls == []


async def test_onebot_batch3_actions_advertised() -> None:
    adapter, _ = _onebot_with_recorder()
    for action in (
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
    ):
        assert action in adapter.actions, action


# --------------------------------------------------------------------------- #
# 动作 4 批：群管理补充 / 社交 / 群文件系统
# --------------------------------------------------------------------------- #
async def test_group_management_actions_route() -> None:
    adapter = FakeAdapter()
    cap = _cap(adapter)
    assert await cap.set_portrait("https://example.invalid/a.png", channel=GROUP) is True
    assert await cap.set_anonymous(False, channel=GROUP) is True
    assert await cap.ban_anonymous("flag-1", 300, channel=GROUP) is True
    assert adapter.seen == [
        ("set_portrait", ("100", "https://example.invalid/a.png")),
        ("set_anonymous", ("100", False)),
        ("ban_anonymous", ("100", "flag-1", 300)),
    ]


async def test_social_actions_route() -> None:
    adapter = FakeAdapter()
    cap = _cap(adapter)
    assert await cap.user_status("u1") == {"status": 10, "ext_status": 0}
    assert await cap.unidirectional_friends() == [{"user_id": 7, "nickname": "单向"}]
    assert await cap.sign_info(channel=GROUP) == {"group_id": "100", "sign_count": 3}
    assert await cap.sign_in(channel=GROUP) is True
    assert [name for name, _ in adapter.seen] == [
        "user_status",
        "unidirectional_friends",
        "sign_info",
        "sign_in",
    ]


async def test_group_file_tree_routes() -> None:
    adapter = FakeAdapter()
    cap = _cap(adapter)
    tree = await cap.group_files(channel=GROUP)
    assert tree["files"][0]["name"] == "a.txt"
    assert tree["folders"][0]["id"] == "d1"
    assert await cap.folder_files("d1", channel=GROUP) == {"files": [], "folders": []}
    url = await cap.file_url("f1", 102, channel=GROUP)
    assert url is not None and url.endswith("busid=102")
    info = await cap.file_system_info(channel=GROUP)
    assert info == {"file_count": 1, "limit_count": 10000}


async def test_group_file_write_actions_are_audited() -> None:
    adapter = FakeAdapter()
    audit = _Audit()
    cap = PlatformCap("test", adapters={"onebot": adapter}, audit=audit)

    assert await cap.create_folder("备份", channel=GROUP) == "d-new"
    assert await cap.delete_file("f1", 102, channel=GROUP) is True
    assert await cap.delete_folder("d1", channel=GROUP) is True
    assert await cap.set_portrait("a.png", channel=GROUP) is True
    assert await cap.set_anonymous(True, channel=GROUP) is True
    assert await cap.ban_anonymous("flag-1", channel=GROUP) is True
    assert await cap.sign_in(channel=GROUP) is True

    assert [call["action"] for call in audit.calls] == [
        "platform:create_folder",
        "platform:delete_file",
        "platform:delete_folder",
        "platform:set_portrait",
        "platform:set_anonymous",
        "platform:ban_anonymous",
        "platform:sign_in",
    ]


async def test_onebot_batch4_action_names() -> None:
    adapter, calls = _onebot_with_recorder()

    await adapter.set_portrait(GROUP, "https://example.invalid/a.png")
    await adapter.set_anonymous(GROUP, True)
    await adapter.ban_anonymous(GROUP, "flag-1", 300)
    await adapter.user_status("u1")
    await adapter.unidirectional_friends()
    await adapter.sign_info(GROUP)
    await adapter.sign_in(GROUP)
    await adapter.group_files(GROUP)
    await adapter.folder_files(GROUP, "d1")
    await adapter.file_url(GROUP, "f1", 102)
    await adapter.file_system_info(GROUP)
    await adapter.create_folder(GROUP, "备份")
    await adapter.delete_file(GROUP, "f1", 102)
    await adapter.delete_folder(GROUP, "d1")

    assert [c[0] for c in calls] == [
        "set_group_portrait",
        "set_group_anonymous",
        "set_group_anonymous_ban",
        "get_user_status",
        "get_unidirectional_friend_list",
        "get_group_sign",
        "send_group_sign",
        "get_group_root_files",
        "get_group_files_by_folder",
        "get_group_file_url",
        "get_group_file_system_info",
        "create_group_file_folder",
        "delete_group_file",
        "delete_group_folder",
    ]
    assert calls[0][1] == {"group_id": 100, "file": "https://example.invalid/a.png"}
    assert calls[1][1] == {"group_id": 100, "enable": True}
    assert calls[2][1] == {"group_id": 100, "anonymous_flag": "flag-1", "duration": 300}
    assert calls[9][1] == {"group_id": 100, "file_id": "f1", "busid": 102}
    # 空 parent_id 会被补成根目录 "/"
    assert calls[11][1] == {"group_id": 100, "name": "备份", "parent_id": "/"}
    assert calls[12][1] == {"group_id": 100, "file_id": "f1", "busid": 102}


def test_onebot_file_tree_keeps_raw_fields() -> None:
    """归一化只**加**别名，不丢协议端原始字段。"""
    raw = {
        "files": [
            {
                "file_id": "f1",
                "file_name": "a.txt",
                "file_size": 3,
                "busid": 102,
                "upload_time": 1700000000,
                "uploader_name": "小明",
            },
            "garbage",
        ],
        "folders": [
            {"folder_id": "d1", "folder_name": "子目录", "total_file_count": 2},
        ],
    }
    tree = OneBotAdapter._file_tree(raw)
    assert len(tree["files"]) == 1
    item = tree["files"][0]
    assert item["id"] == "f1" and item["name"] == "a.txt" and item["size"] == 3
    assert item["busid"] == 102 and item["uploader_name"] == "小明"
    assert tree["folders"][0]["count"] == 2
    # 空 / 异常输入不炸
    assert OneBotAdapter._file_tree(None) == {"files": [], "folders": []}


async def test_onebot_anonymous_ban_requires_flag() -> None:
    adapter, calls = _onebot_with_recorder()
    with pytest.raises(AdapterError):
        await adapter.ban_anonymous(GROUP, "")
    assert calls == []


async def test_onebot_batch4_rejects_private_chat() -> None:
    adapter, _ = _onebot_with_recorder()
    private = ChannelId("onebot", ChannelKind.PRIVATE, "u1")
    for call in (
        adapter.set_portrait(private, "a.png"),
        adapter.set_anonymous(private, True),
        adapter.ban_anonymous(private, "flag"),
        adapter.sign_in(private),
        adapter.create_folder(private, "x"),
        adapter.delete_file(private, "f1", 1),
        adapter.delete_folder(private, "d1"),
    ):
        with pytest.raises(AdapterError):
            await call


async def test_onebot_batch4_actions_advertised() -> None:
    adapter, _ = _onebot_with_recorder()
    for action in (
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
    ):
        assert action in adapter.actions, action


# --------------------------------------------------------------------------- #
# 收尾：can_send_* / 群系统消息 / 删除文件夹别名
# --------------------------------------------------------------------------- #
async def test_can_send_and_system_messages_route() -> None:
    adapter = FakeAdapter()
    cap = _cap(adapter)
    assert await cap.can_send_image() is True
    assert await cap.can_send_record(GROUP) is False
    assert await cap.system_messages(GROUP) == {"invited_requests": [], "join_requests": []}


async def test_onebot_can_send_and_system_msg_names() -> None:
    adapter, calls = _onebot_with_recorder()
    await adapter.can_send_image(GROUP)
    await adapter.can_send_record()
    await adapter.system_messages(GROUP)
    assert [c[0] for c in calls] == ["can_send_image", "can_send_record", "get_group_system_msg"]
    # 群聊带上 group_id，不带频道时参数为空
    assert calls[0][1] == {"group_id": 100}
    assert calls[1][1] == {}


async def test_onebot_delete_folder_falls_back_to_alias() -> None:
    """协议端只认老名字时自动重试 delete_group_file_folder。"""
    adapter, calls = _onebot_with_recorder()

    async def fake_call(action: str, params: dict, *, timeout: float | None = None) -> dict:
        calls.append((action, params))
        if action == "delete_group_folder":
            raise AdapterError("OneBot delete_group_folder 失败（retcode=1404）：不支持的API")
        return {"status": "ok", "data": {}}

    adapter.call = fake_call  # type: ignore[method-assign]
    assert await adapter.delete_folder(GROUP, "d1") is True
    assert [c[0] for c in calls] == ["delete_group_folder", "delete_group_file_folder"]
    assert calls[0][1] == calls[1][1]


async def test_onebot_batch5_actions_advertised() -> None:
    adapter, _ = _onebot_with_recorder()
    for action in ("can_send_image", "can_send_record", "system_messages"):
        assert action in adapter.actions, action
