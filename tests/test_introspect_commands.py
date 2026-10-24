"""/help 的命令说明口径：命令没写 description 时不能回落成插件描述。

起因：群里 /help 出现「/画图 <描述> —— 对话 + 按需出图 + 内容审查」，因为
registry.commands() 里写了 `rule.description or handle.manifest.description`。
每条命令都挂同一句插件描述，等于没有说明。
"""

from __future__ import annotations

from types import SimpleNamespace

from haiqbot.kernel.matcher import MatchRule
from haiqbot.kernel.registry import Registry


def make_handle(*rules: MatchRule) -> SimpleNamespace:
    return SimpleNamespace(
        rules=list(rules),
        manifest=SimpleNamespace(name="助手", description="插件级描述（不该出现在命令后面）"),
    )


def commands_of(*rules: MatchRule) -> list[dict]:
    reg = Registry.__new__(Registry)  # 只测 commands()，不跑 __init__
    reg._handles = {"assistant": make_handle(*rules)}
    return reg.commands()


def rule(command: str, *, description: str | None = None) -> MatchRule:
    return MatchRule(
        plugin_id="assistant",
        func=lambda ctx: None,
        commands=(command,),
        usage=f"{command} <x>",
        description=description,
    )


def test_command_without_description_is_empty_not_plugin_description():
    row = commands_of(rule("画图"))[0]
    assert row["description"] == ""


def test_command_with_description_uses_its_own():
    row = commands_of(rule("画图", description="强制出图"))[0]
    assert row["description"] == "强制出图"


def test_plugin_name_is_still_available_for_grouping():
    """分组用的插件名不能被一起砍掉 —— /help 要靠它分节。"""
    row = commands_of(rule("问"))[0]
    assert row["plugin_name"] == "助手"
