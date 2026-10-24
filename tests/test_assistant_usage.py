"""assistant 插件：feature 级开关 + 今日用量面板页。

两件事各自对应一个真实的坑：

1. 四个入口必须声明 `feature=`，否则频道开关只能整插件开关，
   「这群只许对话、不许画图」做不到（`bus.rule_allowed` 支持 `assistant:draw`，
   但没声明 feature 就等于没这个开关）。
2. 面板页的「今日」必须落盘：进程重启后数字不该归零，跨天则要自动归零。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from haiqbot_local_assistant import Assistant

from haiqbot.plugin_api import get_logger

LOG = get_logger("test.assistant")


class FakeKV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str, default: str | None = None) -> str | None:
        return self.data.get(key, default)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


def ctx_with(kv: FakeKV | None = None, *, actor: str = "u1") -> SimpleNamespace:
    return SimpleNamespace(
        event=SimpleNamespace(
            actor=SimpleNamespace(id=actor),
            channel="onebot:group:1",
            text="hi",
        ),
        kv=kv if kv is not None else FakeKV(),
        config={},
        log=LOG,
    )


def features_of(func: object) -> list[str | None]:
    return [spec.get("feature") for spec in getattr(func, "_haiqbot_specs", [])]


# ---------------------------------------------------------------- feature 声明


def test_every_entry_declares_feature():
    """四个入口都必须有 feature，名字固定为 ask / draw / reset。"""
    assert features_of(Assistant.ask) == ["ask"]
    assert features_of(Assistant.talk) == ["ask"]
    assert features_of(Assistant.draw) == ["draw"]
    assert features_of(Assistant.reset) == ["reset"]


def test_feature_names_are_stable():
    """频道配置里存的是 `assistant:draw` 这种字符串 —— 改名等于改数据契约。"""
    declared = {
        *features_of(Assistant.ask),
        *features_of(Assistant.talk),
        *features_of(Assistant.draw),
        *features_of(Assistant.reset),
    }
    assert declared == {"ask", "draw", "reset"}


# ---------------------------------------------------------------- 面板页


def field_value(page: dict, label: str) -> object:
    for section in page["sections"]:
        if section.get("type") != "field_group":
            continue
        for field in section["fields"]:
            if field["label"] == label:
                return field["value"]
    raise AssertionError(f"面板里没有字段 {label}")


def table_rows(page: dict) -> list[dict]:
    for section in page["sections"]:
        if section.get("type") == "readonly_table":
            return section["rows"]
    raise AssertionError("面板里没有表格")


async def test_bump_then_page_reflects_counters():
    plugin = Assistant()
    ctx = ctx_with()

    await plugin._bump(ctx, "draw_ok", actor="u1", kind="draw")
    await plugin._bump(ctx, "draw_fail", actor="u1")
    await plugin._bump(ctx, "ask_ok", actor="u2", kind="ask")
    await plugin._bump(ctx, "blocked", actor="u3")

    page = await plugin.panel_page(SimpleNamespace(config={"router_mode": "auto"}))

    assert field_value(page, "出图成功") == 1
    assert field_value(page, "出图失败") == 1
    assert field_value(page, "对话回复") == 1
    assert field_value(page, "审查命中") == 1
    assert field_value(page, "路由方式") == "auto"

    rows = table_rows(page)
    assert rows[0]["user"] == "u1"  # 出图数优先排序
    assert rows[0]["draw"] == 1
    assert rows[0]["total"] == 1


async def test_page_is_empty_state_safe():
    plugin = Assistant()
    page = await plugin.panel_page(SimpleNamespace(config={}))
    assert table_rows(page)[0]["user"].startswith("（今天还没有人用")


async def test_stats_survive_reload():
    """重启后「今日」不该归零：on_load 要把当天记录读回来。"""
    kv = FakeKV()
    first = Assistant()
    ctx = ctx_with(kv)
    await first._bump(ctx, "draw_ok", actor="u1", kind="draw")

    second = Assistant()
    await second.on_load(ctx_with(kv))
    page = await second.panel_page(SimpleNamespace(config={}))

    assert field_value(page, "出图成功") == 1


async def test_stale_record_from_yesterday_is_ignored():
    """kv 里留着昨天的记录：不能把它当今天的数字显示出来。"""
    kv = FakeKV()
    kv.data["stats:daily"] = json.dumps(
        {"date": "2000-01-01", "draw_ok": 99, "users": {"u1": {"draw": 99, "ask": 0}}}
    )

    plugin = Assistant()
    await plugin.on_load(ctx_with(kv))
    page = await plugin.panel_page(SimpleNamespace(config={}))

    assert field_value(page, "出图成功") == 0


async def test_reset_action_clears_memory_and_kv():
    kv = FakeKV()
    plugin = Assistant()
    ctx = ctx_with(kv)
    await plugin.on_load(ctx)
    await plugin._bump(ctx, "draw_ok", actor="u1", kind="draw")

    result = await plugin.panel_action("reset", {})

    assert "清零" in str(result)
    page = await plugin.panel_page(SimpleNamespace(config={}))
    assert field_value(page, "出图成功") == 0
    saved = json.loads(kv.data["stats:daily"])
    assert saved["draw_ok"] == 0


async def test_unknown_action_is_rejected():
    plugin = Assistant()
    with pytest.raises(KeyError):
        await plugin.panel_action("drop_all_tables", {})
