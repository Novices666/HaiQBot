"""guard 插件：刷屏限流 / 广告拦截 / 管理员豁免，以及 always 语义的前提。"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from haiqbot.builtin_plugins.guard import Guard
from haiqbot.plugin_api import Role


class MappingOnly(Mapping):
    """模拟内核的 LiveConfig：是 Mapping，但**不是 dict**。

    曾经因为 `isinstance(cfg, dict)` 不成立，guard 静默回落到默认配置，
    结果"改配置等于没改" —— 这个测试就是钉死这个坑。
    """

    def __init__(self, data: dict) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class FakeKV:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str, default: str | None = None) -> str | None:
        return self.data.get(key, default)

    async def set(self, key: str, value: str) -> None:
        self.data[key] = value


class FakeReply:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def text(self, text: str) -> None:
        self.texts.append(text)


class Ctx:
    def __init__(self, text: str, *, role: Role = Role.USER, config: dict | None = None) -> None:
        self.event = SimpleNamespace(
            text=text,
            actor=SimpleNamespace(id="u1", role=role),
            channel="onebot:group:1",
        )
        self.kv = FakeKV()
        self.reply = FakeReply()
        self.config = config or {}
        self.log = SimpleNamespace(info=lambda *a, **k: None)


def test_plugin_declares_always_gating():
    """guard 的全部意义就在这一行：治理插件不受频道开关约束。"""
    assert Guard.gating == "always"


@pytest.mark.asyncio
async def test_plain_message_is_passed_through():
    guard = Guard()
    ctx = Ctx("今天天气不错")
    assert await guard.watch(ctx) is False  # False = 放行，不计消费
    assert ctx.reply.texts == []


@pytest.mark.asyncio
async def test_link_is_blocked():
    guard = Guard()
    ctx = Ctx("看这个 https://spam.example.com/x")
    assert await guard.watch(ctx) is True
    assert guard._stats["by_reason"] == {"广告链接": 1}


@pytest.mark.asyncio
async def test_flood_is_blocked_after_limit():
    guard = Guard()
    ctx = Ctx("刷屏测试", config={"window_seconds": 60, "max_messages": 3})
    verdicts = [await guard.watch(ctx) for _ in range(5)]
    assert verdicts[:3] == [False, False, False]  # 3 条以内放行
    assert verdicts[3:] == [True, True]  # 超限拦截
    assert ctx.reply.texts  # 至少提示一次
    assert guard._stats["by_reason"]["刷屏"] == 2


@pytest.mark.asyncio
async def test_blocked_word():
    guard = Guard()
    ctx = Ctx("这里有代孕广告", config={"blocked_words": "代孕,博彩"})
    assert await guard.watch(ctx) is True
    assert "违禁词" in next(iter(guard._stats["by_reason"]))


@pytest.mark.asyncio
async def test_admin_is_exempt():
    """管理员免检：误伤时必须留一条人工通道。"""
    guard = Guard()
    ctx = Ctx("https://spam.example.com/x", role=Role.ADMIN)
    assert await guard.watch(ctx) is False
    assert guard._stats["total"] == 0


@pytest.mark.asyncio
async def test_admin_can_be_blocked_when_exemption_off():
    guard = Guard()
    ctx = Ctx("https://spam.example.com/x", role=Role.ADMIN, config={"exempt_admins": False})
    assert await guard.watch(ctx) is True


@pytest.mark.asyncio
async def test_warning_is_rate_limited_per_user():
    """拦截提示 30 秒内只发一次，避免提示本身变成刷屏。"""
    guard = Guard()
    ctx = Ctx("https://a.example.com/1", config={"max_messages": 99})
    await guard.watch(ctx)
    await guard.watch(ctx)
    assert len(ctx.reply.texts) == 1


def test_panel_page_reflects_stats():
    guard = Guard()
    guard._stats = {"total": 7, "by_reason": {"刷屏": 5, "广告链接": 2}, "recent": []}
    page = guard.panel_page(SimpleNamespace(config={}))
    kinds = [section["type"] for section in page["sections"]]
    assert kinds == ["text", "field_group", "readonly_table", "readonly_table", "action_button"]
    assert page["sections"][2]["rows"][0] == {"reason": "刷屏", "count": 5}


def test_panel_action_reset():
    guard = Guard()
    guard._stats = {"total": 3, "by_reason": {"刷屏": 3}, "recent": [{"ts": 1}]}
    assert guard.panel_action("reset", {})["message"] == "统计已清零"
    assert guard._stats["total"] == 0
    with pytest.raises(KeyError):
        guard.panel_action("nope", {})


@pytest.mark.asyncio
async def test_config_accepts_mapping_not_just_dict():
    """ctx.config 是 LiveConfig（Mapping）而不是 dict —— 必须照样读得到。"""
    guard = Guard()
    ctx = Ctx("https://spam.example.com/x", role=Role.ADMIN)
    ctx.config = MappingOnly({"exempt_admins": False, "block_links": True})
    assert await guard.watch(ctx) is True


@pytest.mark.asyncio
async def test_mapping_config_exempt_still_works():
    guard = Guard()
    ctx = Ctx("https://spam.example.com/x", role=Role.OWNER)
    ctx.config = MappingOnly({"exempt_admins": True})
    assert await guard.watch(ctx) is False
