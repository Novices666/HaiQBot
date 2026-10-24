"""Reply 的审计摘要：只留前 80 字 + 长度，不把群聊全文写进库里。

起因：群里报“它回得不对”时，审计只记了 handled，日志只有 chars=154，
最后是靠 NapCat 的 get_group_msg_history 去群里捞才看到回复原文。
"""

from __future__ import annotations

from haiqbot.adapters.mock import MockAdapter
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.reply import Reply


def make_reply() -> Reply:
    adapter = MockAdapter()
    channel = ChannelId("mock", ChannelKind.GROUP, "1")
    return Reply(adapter, channel, capabilities=adapter.capabilities)


def test_summary_is_none_before_any_send():
    assert make_reply().summary() is None


async def test_summary_keeps_preview_and_total_length():
    reply = make_reply()
    await reply.text("嗯" * 200)

    summary = reply.summary()
    assert summary is not None
    assert summary["messages"] == 1
    assert summary["chars"] == 200
    assert len(summary["preview"]) == 80  # 截断，不落全文


async def test_summary_merges_multiple_sends():
    reply = make_reply()
    await reply.text("第一句")
    await reply.text("第二句")

    summary = reply.summary()
    assert summary is not None
    assert summary["messages"] == 2
    assert summary["chars"] == 6
    assert "第一句" in summary["preview"]
    assert "第二句" in summary["preview"]


async def test_summary_records_segment_kinds():
    reply = make_reply()
    await reply.image("https://example.com/a.png")

    summary = reply.summary()
    assert summary is not None
    assert summary["kinds"] == ["image"]
    assert summary["chars"] == 0
    assert summary["preview"] == ""


async def test_expired_reply_does_not_record():
    """窗口过期时根本没发出去，不该在审计里留下“发过”的痕迹。"""
    import time

    adapter = MockAdapter()
    channel = ChannelId("mock", ChannelKind.GROUP, "1")
    reply = Reply(adapter, channel, capabilities=adapter.capabilities, deadline=time.time() - 1)

    await reply.text("发不出去的")

    assert reply.summary() is None
