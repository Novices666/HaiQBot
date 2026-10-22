"""声明式匹配器。"""

from __future__ import annotations

import re

from haiqbot.core.channel import Actor, ChannelId, ChannelKind, Role
from haiqbot.core.event import EventKind, MessageEvent
from haiqbot.core.segment import Segment
from haiqbot.kernel.matcher import MatchRule


def _event(text: str, *, role: Role = Role.USER, platform: str = "mock", at: bool = False) -> MessageEvent:
    return MessageEvent(
        kind=EventKind.MESSAGE,
        channel=ChannelId(platform, ChannelKind.GROUP, "1"),
        actor=Actor(id="u1", role=role),
        segments=[Segment.text(text)],
        msg_id="m1",
        at_bot=at,
    )


async def _noop(ctx: object, prompt: str = "") -> None: ...


def test_command_matches_with_and_without_slash() -> None:
    rule = MatchRule(plugin_id="p", func=_noop, commands=("画图",))
    assert rule.matches(_event("/画图 a cat")).matched is True
    assert rule.matches(_event("画图 a cat")).matched is True
    assert rule.matches(_event("画图abc")).matched is False


def test_alias_matches() -> None:
    rule = MatchRule(plugin_id="p", func=_noop, commands=("画图", "draw"))
    assert rule.matches(_event("/draw cat")).matched is True


def test_last_param_absorbs_rest() -> None:
    rule = MatchRule(plugin_id="p", func=_noop, commands=("画图",))
    out = rule.matches(_event("/画图 一只 橘色 的猫"))
    kwargs = rule.build_kwargs(out, "/画图 一只 橘色 的猫")
    assert kwargs["prompt"] == "一只 橘色 的猫"


def test_role_gate() -> None:
    rule = MatchRule(plugin_id="p", func=_noop, commands=("admin",), min_role=Role.ADMIN)
    assert rule.matches(_event("/admin")).matched is False
    assert rule.matches(_event("/admin", role=Role.ADMIN)).matched is True


def test_platform_filter() -> None:
    rule = MatchRule(plugin_id="p", func=_noop, commands=("x",), platforms=frozenset({"official"}))
    assert rule.matches(_event("/x", platform="mock")).matched is False
    assert rule.matches(_event("/x", platform="official")).matched is True


def test_require_at() -> None:
    rule = MatchRule(plugin_id="p", func=_noop, commands=("x",), require_at=True)
    assert rule.matches(_event("/x", at=False)).matched is False
    assert rule.matches(_event("/x", at=True)).matched is True


def test_regex_rule_passes_match() -> None:
    async def handler(ctx: object, match: re.Match[str]) -> None: ...

    rule = MatchRule(plugin_id="p", func=handler, pattern=re.compile(r"^投票 (\w+)$"))
    out = rule.matches(_event("投票 披萨"))
    assert out.matched is True
    kwargs = rule.build_kwargs(out, "投票 披萨")
    assert kwargs["match"].group(1) == "披萨"


def test_notice_event_does_not_match_command_rule() -> None:
    from haiqbot.core.event import NoticeEvent

    rule = MatchRule(plugin_id="p", func=_noop, commands=("x",))
    notice = NoticeEvent(
        kind=EventKind.NOTICE,
        channel=ChannelId("mock", ChannelKind.GROUP, "1"),
        actor=Actor(id="u1"),
        notice_id="n1",
    )
    assert rule.matches(notice).matched is False
