"""OneBot 事件面 + 管理动作。

覆盖三件容易出错、又最影响「能不能写管理插件」的事：
1. 通知/请求事件真的被解析出来（而不是 `post_type != message` 就丢掉）；
2. 撤回/禁言/踢人/审批发出的 action 与参数和 OneBot v11 一致；
3. 权限边界：请求审批默认要管理员，普通群员触发不了。
"""

from __future__ import annotations

from typing import Any

import pytest

from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import Actor, ChannelId, ChannelKind, Role
from haiqbot.core.errors import AdapterError, CapabilityUnavailable
from haiqbot.core.event import EventKind, MessageEvent, NoticeEvent, RequestEvent
from haiqbot.kernel.capabilities import ModerationCap
from haiqbot.kernel.matcher import MatchRule
from haiqbot.kernel.plugin import _as_frozenset


def _adapter(push: Any = None) -> OneBotAdapter:
    return OneBotAdapter(OneBotCfg(), token_provider=lambda: None, push=push)


GROUP = ChannelId("onebot", ChannelKind.GROUP, "200000001")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #
def test_group_message_keeps_group_id_in_raw() -> None:
    event = _adapter()._parse_message(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": 200000001,
            "user_id": 100000001,
            "message_id": 42,
            "message": "你好",
            "sender": {"nickname": "小明"},
        }
    )
    assert isinstance(event, MessageEvent)
    assert event.channel.kind is ChannelKind.GROUP
    assert event.channel.id == "200000001"
    # raw 现在忠实保留原始上报：group_id 是协议端的 int，channel.id 才是规范化后的 str
    assert event.raw["group_id"] == 200000001, "插件常要原始群号，别丢"
    assert "sender" in event.raw and event.raw["sender"]["nickname"] == "小明"
    assert event.text == "你好"


def test_group_increase_notice() -> None:
    event = _adapter()._parse_notice(
        {
            "post_type": "notice",
            "notice_type": "group_increase",
            "sub_type": "approve",
            "group_id": 200000001,
            "user_id": 111,
            "operator_id": 222,
        }
    )
    assert isinstance(event, NoticeEvent)
    assert event.kind is EventKind.NOTICE
    assert event.channel.kind is ChannelKind.GROUP
    assert event.notice_type == "group_increase"
    assert event.sub_type == "approve"
    assert event.actor.id == "111", "当事人是入群的人"
    assert event.operator_id == "222", "操作者是审批的管理员"
    assert event.by_admin is True


def test_recall_notice_carries_message_id() -> None:
    event = _adapter()._parse_notice(
        {
            "post_type": "notice",
            "notice_type": "group_recall",
            "group_id": 200000001,
            "user_id": 111,
            "operator_id": 111,
            "message_id": 987654,
        }
    )
    assert event is not None
    assert event.notice_id == "987654", "撤回事件里 notice_id 就是被撤回的消息"
    assert event.by_admin is False, "自己撤自己的不算管理员操作"


def test_poke_uses_sender_as_operator() -> None:
    event = _adapter()._parse_notice(
        {
            "post_type": "notice",
            "notice_type": "notify",
            "sub_type": "poke",
            "group_id": 200000001,
            "user_id": 111,
            "target_id": 111,
            "sender_id": 333,
        }
    )
    assert event is not None
    assert event.operator_id == "333", "poke 的 operator 是 sender_id"


def test_notice_without_channel_is_dropped() -> None:
    assert _adapter()._parse_notice({"post_type": "notice", "notice_type": "x"}) is None


def test_group_request() -> None:
    event = _adapter()._parse_request(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": 200000001,
            "user_id": 111,
            "comment": "求进群",
            "flag": "abc123",
        }
    )
    assert isinstance(event, RequestEvent)
    assert event.kind is EventKind.REQUEST
    assert event.flag == "abc123"
    assert event.comment == "求进群"
    assert event.group_id == "200000001"
    assert event.channel.kind is ChannelKind.GROUP


def test_friend_request_targets_private_channel() -> None:
    event = _adapter()._parse_request(
        {"post_type": "request", "request_type": "friend", "user_id": 111, "flag": "f1"}
    )
    assert event is not None
    assert event.channel.kind is ChannelKind.PRIVATE
    assert event.channel.id == "111"


def test_heartbeat_is_swallowed_but_updates_bot_id() -> None:
    adapter = _adapter()
    event = adapter._parse_meta(
        {"post_type": "meta_event", "meta_event_type": "heartbeat", "self_id": 100000001}
    )
    assert event is None, "心跳每 30s 一条，不该进管线"
    assert adapter.bot_id == "100000001", "但要顺手记下自己的号"


def test_lifecycle_meta_event_passes_through() -> None:
    event = _adapter()._parse_meta({"post_type": "meta_event", "meta_event_type": "lifecycle", "self_id": 1})
    assert event is not None
    assert event.kind is EventKind.META


async def test_handle_event_dispatches_notice_and_ignores_message_sent() -> None:
    got: list[Any] = []

    async def push(event: Any) -> None:
        got.append(event)

    adapter = _adapter(push)
    await adapter._handle_event(
        {
            "post_type": "notice",
            "notice_type": "group_ban",
            "sub_type": "ban",
            "group_id": 200000001,
            "user_id": 111,
            "operator_id": 222,
            "duration": 600,
        }
    )
    await adapter._handle_event(
        {"post_type": "message_sent", "message_type": "group", "group_id": 1, "user_id": 2}
    )
    assert len(got) == 1
    assert got[0].notice_type == "group_ban"
    assert got[0].raw["duration"] == 600


# --------------------------------------------------------------------------- #
# 管理动作
# --------------------------------------------------------------------------- #
def _recording(adapter: OneBotAdapter) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def fake_call(action: str, params: dict, **_kw: Any) -> dict:
        calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": []}

    adapter.call = fake_call  # type: ignore[method-assign]
    return calls


async def test_recall_sends_delete_msg() -> None:
    adapter = _adapter()
    calls = _recording(adapter)
    assert await adapter.recall(GROUP, "987654") is True
    assert calls == [("delete_msg", {"message_id": 987654})]


async def test_mute_requires_group() -> None:
    adapter = _adapter()
    _recording(adapter)
    with pytest.raises(AdapterError):
        await adapter.mute(ChannelId("onebot", ChannelKind.PRIVATE, "111"), "111", 60)


async def test_mute_sends_group_ban_with_seconds() -> None:
    adapter = _adapter()
    calls = _recording(adapter)
    assert await adapter.mute(GROUP, "111", 600) is True
    assert calls[0][0] == "set_group_ban"
    assert calls[0][1]["duration"] == 600
    assert calls[0][1]["user_id"] == 111
    # 0 秒 = 解除禁言，原样透传，不能被 `or` 吞掉
    await adapter.mute(GROUP, "111", 0)
    assert calls[1][1]["duration"] == 0


async def test_kick_sends_group_kick() -> None:
    adapter = _adapter()
    calls = _recording(adapter)
    assert await adapter.kick(GROUP, "111", reject_add_request=True) is True
    assert calls[0] == (
        "set_group_kick",
        {"group_id": 200000001, "user_id": 111, "reject_add_request": True},
    )


async def test_member_list_normalises_card_and_role() -> None:
    adapter = _adapter()

    async def fake_call(action: str, params: dict, **_kw: Any) -> dict:
        assert action == "get_group_member_list"
        return {
            "data": [
                {"user_id": 1, "nickname": "昵称A", "card": "名片A", "role": "admin"},
                {"user_id": 2, "nickname": "昵称B", "card": "", "role": "member"},
            ]
        }

    adapter.call = fake_call  # type: ignore[method-assign]
    rows = await adapter.member_list(GROUP, limit=10)
    assert rows[0] == {"user_id": "1", "name": "名片A", "role": "admin"}
    assert rows[1]["name"] == "昵称B", "没有群名片就退回昵称"


async def test_handle_group_request_passes_flag_and_sub_type() -> None:
    adapter = _adapter()
    calls = _recording(adapter)
    event = RequestEvent(
        kind=EventKind.REQUEST,
        channel=GROUP,
        actor=Actor(id="111"),
        request_type="group",
        sub_type="add",
        flag="abc123",
    )
    assert await adapter.handle_request(event, True) is True
    assert calls[0] == (
        "set_group_add_request",
        {"flag": "abc123", "sub_type": "add", "approve": True},
    )
    # 拒绝时带理由
    await adapter.handle_request(event, False, reason="人满了")
    assert calls[1][1]["reason"] == "人满了"


async def test_handle_request_without_flag_is_rejected() -> None:
    adapter = _adapter()
    _recording(adapter)
    event = RequestEvent(kind=EventKind.REQUEST, channel=GROUP, actor=Actor(id="1"))
    with pytest.raises(AdapterError):
        await adapter.handle_request(event, True)


def test_onebot_declares_its_actions() -> None:
    assert {"recall", "mute", "kick", "member_list", "request"} <= _adapter().actions
    assert _adapter().describe()["actions"], "面板要能看到平台能力"


# --------------------------------------------------------------------------- #
# 能力句柄
# --------------------------------------------------------------------------- #
async def test_moderation_cap_routes_and_audits() -> None:
    audited: list[tuple[str, str]] = []

    class FakeAudit:
        async def record(self, **kw: Any) -> None:
            audited.append((kw["action"], kw["detail"]))

    adapter = _adapter()
    _recording(adapter)
    cap = ModerationCap("guard", {"onebot": adapter}, FakeAudit())

    assert cap.supports("mute", GROUP) is True
    assert cap.supports("nonsense") is False
    assert await cap.mute(GROUP, "111", 60) is True
    assert audited and audited[0][0] == "moderation:mute"
    assert "seconds=60" in audited[0][1]


async def test_moderation_cap_unknown_platform() -> None:
    cap = ModerationCap("guard", {})
    with pytest.raises(CapabilityUnavailable):
        await cap.mute(GROUP, "111", 60)
    assert cap.supports("mute", GROUP) is False


async def test_moderation_cap_audit_failure_does_not_block() -> None:
    class BadAudit:
        async def record(self, **_kw: Any) -> None:
            raise RuntimeError("db down")

    adapter = _adapter()
    _recording(adapter)
    cap = ModerationCap("guard", {"onebot": adapter}, BadAudit())
    assert await cap.recall(GROUP, "1") is True


# --------------------------------------------------------------------------- #
# 匹配与权限
# --------------------------------------------------------------------------- #
def _rule(**kw: Any) -> MatchRule:
    async def handler(ctx: Any) -> None: ...

    return MatchRule(plugin_id="guard", func=handler, **kw)


def _notice(notice_type: str, sub_type: str = "") -> NoticeEvent:
    return NoticeEvent(
        kind=EventKind.NOTICE,
        channel=GROUP,
        actor=Actor(id="111"),
        notice_type=notice_type,
        sub_type=sub_type,
    )


def test_notice_type_filter() -> None:
    rule = _rule(
        events=frozenset({EventKind.NOTICE}),
        notice_types=_as_frozenset("group_increase"),
    )
    assert rule.matches(_notice("group_increase")).matched is True
    assert rule.matches(_notice("group_recall")).matched is False


def test_notice_sub_type_filter_accepts_list() -> None:
    rule = _rule(
        events=frozenset({EventKind.NOTICE}),
        notice_sub_types=_as_frozenset(["ban", "lift_ban"]),
    )
    assert rule.matches(_notice("group_ban", "ban")).matched is True
    assert rule.matches(_notice("group_ban", "kick")).matched is False


def test_request_rule_defaults_to_admin() -> None:
    from haiqbot.kernel.plugin import on_request

    @on_request(request_type="group")
    async def _handler(self: Any, ctx: Any) -> None: ...

    spec = _handler._haiqbot_specs[0]
    assert spec["min_role"] is Role.ADMIN, "审批默认要管理员，不能靠插件自觉"


def test_notice_rule_defaults_to_user() -> None:
    from haiqbot.kernel.plugin import on_notice

    @on_notice(notice_type="group_increase")
    async def _handler(self: Any, ctx: Any) -> None: ...

    spec = _handler._haiqbot_specs[0]
    assert spec["min_role"] is Role.USER
    assert spec["events"] == (EventKind.NOTICE,)
    assert spec["notice_types"] == frozenset({"group_increase"})


def test_request_event_never_matches_message_rules() -> None:
    rule = _rule()  # 默认只吃 MESSAGE
    event = RequestEvent(kind=EventKind.REQUEST, channel=GROUP, actor=Actor(id="1"), request_type="group")
    assert rule.matches(event).matched is False


def test_sender_role_is_mapped() -> None:
    """群主/管理员身份必须落到 Actor.role —— 否则风控会把管理层也拦了。"""
    base = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 42,
        "message_id": 7,
        "message": [{"type": "text", "data": {"text": "hi"}}],
        "self_id": 999,
    }
    for raw_role, expected in (("owner", Role.OWNER), ("admin", Role.ADMIN), ("member", Role.USER)):
        payload = {**base, "sender": {"nickname": "x", "role": raw_role}}
        event = _adapter()._parse_message(payload)
        assert event is not None
        assert event.actor.role is expected


def test_at_bot_is_computed_from_segments() -> None:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 1,
        "user_id": 42,
        "message_id": 7,
        "message": [
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 画一只猫"}},
        ],
        "self_id": 999,
        "sender": {"nickname": "x", "role": "member"},
    }
    event = _adapter()._parse_message(payload)
    assert event is not None and event.at_bot is True

    payload["message"] = [{"type": "text", "data": {"text": "画一只猫"}}]
    event = _adapter()._parse_message(payload)
    assert event is not None and event.at_bot is False
