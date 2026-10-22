"""功能级开关（`chat:ask`）的放行规则。

channel_config.plugins 里既可能写整插件（`painter`），也可能写单个功能（`chat:ask`）。
这条测试守着那个判定 —— 它决定"关掉一个功能"是不是真的只关了那一个。
"""

from __future__ import annotations

from typing import Any

from haiqbot.kernel.bus import rule_allowed, rule_in_scope
from haiqbot.kernel.matcher import MatchRule


def _rule(feature: str | None = None, pid: str = "chat") -> MatchRule:
    return MatchRule(plugin_id=pid, func=lambda **_: None, feature=feature)  # type: ignore[arg-type]


def test_whole_plugin_in_list_allows_every_rule() -> None:
    allowed = {"chat"}
    assert rule_allowed(_rule(feature="ask"), allowed) is True
    assert rule_allowed(_rule(feature="reset"), allowed) is True
    assert rule_allowed(_rule(feature=None), allowed) is True


def test_feature_only_allows_declared_feature() -> None:
    allowed = {"chat:ask"}
    assert rule_allowed(_rule(feature="ask"), allowed) is True
    assert rule_allowed(_rule(feature="reset"), allowed) is False
    # 没声明 feature 的规则不能被"只开某个功能"放进场
    assert rule_allowed(_rule(feature=None), allowed) is False


def test_other_plugin_never_matches() -> None:
    allowed = {"chat:ask"}
    assert rule_allowed(_rule(feature="ask", pid="painter"), allowed) is False


def test_empty_allowed_blocks_everything() -> None:
    assert rule_allowed(_rule(feature="ask"), set()) is False


def test_feature_reaches_match_rule_through_spec() -> None:
    """装饰器声明的 feature 要能一路传到 MatchRule（否则上面几条都是空的）。"""
    from haiqbot.kernel.plugin import on_command

    @on_command("问", feature="ask")
    async def handler(ctx: Any, **_: Any) -> None:  # pragma: no cover - 不会执行
        return None

    specs = handler._haiqbot_specs
    assert specs[0]["feature"] == "ask"


# --------------------------------------------------------------------------- #
# ③ 频道关闭 + always 豁免
# --------------------------------------------------------------------------- #
def test_closed_channel_blocks_normal_plugin() -> None:
    extra = {"scope_closed": True, "allowed_plugins": set()}
    assert rule_in_scope(_rule(feature="ask"), extra) is False


def test_closed_channel_still_allows_always_plugin() -> None:
    extra = {"scope_closed": True, "allowed_plugins": set(), "always_plugins": {"chat"}}
    assert rule_in_scope(_rule(feature="ask"), extra) is True


def test_open_channel_uses_allow_list() -> None:
    open_extra = {"scope_closed": False, "allowed_plugins": {"chat:ask"}}
    assert rule_in_scope(_rule(feature="ask"), open_extra) is True
    assert rule_in_scope(_rule(feature="reset"), open_extra) is False


def test_no_gate_means_unrestricted() -> None:
    """没有 channel_gate 时（部分测试/嵌入式用法）不限制。"""
    assert rule_in_scope(_rule(feature="ask"), {}) is True
