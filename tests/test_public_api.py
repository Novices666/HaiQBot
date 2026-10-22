"""公开 API 一致性：`haiqbot` 必须完整转发 `plugin_api`，否则文档示例会 ImportError。"""

from __future__ import annotations

import haiqbot
from haiqbot import plugin_api


def test_haiqbot_reexports_everything_from_plugin_api() -> None:
    missing = set(plugin_api.__all__) - set(haiqbot.__all__)
    assert not missing, f"haiqbot 未导出：{sorted(missing)}"


def test_reexported_names_are_the_same_objects() -> None:
    for name in plugin_api.__all__:
        assert getattr(haiqbot, name) is getattr(plugin_api, name), name


def test_documented_entry_points_exist() -> None:
    """PLUGIN-GUIDE 里出现的名字必须真的能从 `haiqbot` 导入。"""
    for name in (
        "Plugin",
        "Context",
        "ProviderContext",
        "SecretSlot",
        "SecretHandle",
        "SecretValue",
        "ServiceSpec",
        "on_command",
        "on_message",
        "on_regex",
        "on_notice",
        "on_request",
        "on_meta",
    ):
        assert hasattr(haiqbot, name), name
