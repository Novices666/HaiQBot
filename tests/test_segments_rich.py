"""富消息段：语音 / 视频 / 表情 / markdown / 按钮 / 合并转发。

两条底线：
1. 平台不支持时**降级但不能丢内容**（markdown → 纯文本，合并转发 → 逐条文本）；
2. 内核不发明平台的按钮 schema，只套外壳、原样透传。
"""

from __future__ import annotations

from typing import Any

import pytest

from haiqbot.adapters.mock import MockAdapter
from haiqbot.adapters.onebot11 import _decode_segments, _encode_segments
from haiqbot.config import OneBotCfg
from haiqbot.core.channel import ChannelId, ChannelKind
from haiqbot.core.reply import PlatformCapabilities
from haiqbot.core.segment import ForwardNode, Segment, SegmentKind, segments_to_text

GROUP = ChannelId("onebot", ChannelKind.GROUP, "674425800")
PRIVATE = ChannelId("onebot", ChannelKind.PRIVATE, "111")


def _enc(segments: list[Segment]) -> list[dict[str, Any]]:
    return _encode_segments(segments, None)


# --------------------------------------------------------------------------- #
# 编解码
# --------------------------------------------------------------------------- #


def test_encode_media_and_face() -> None:
    out = _enc(
        [
            Segment.record("https://x/a.silk"),
            Segment.video("https://x/a.mp4"),
            Segment.face(14),
            Segment.at("all"),
        ]
    )
    assert out[0] == {"type": "record", "data": {"file": "https://x/a.silk"}}
    assert out[1] == {"type": "video", "data": {"file": "https://x/a.mp4"}}
    assert out[2] == {"type": "face", "data": {"id": "14"}}
    assert out[3] == {"type": "at", "data": {"qq": "all"}}


def test_encode_markdown_degrades_to_text() -> None:
    out = _enc([Segment.markdown("**加粗**")])
    assert out == [{"type": "text", "data": {"text": "**加粗**"}}], "内容不能丢"


def test_encode_keyboard_is_dropped() -> None:
    assert _enc([Segment.keyboard([{"buttons": []}])]) == []


def test_decode_rich_segments() -> None:
    segs = _decode_segments(
        [
            {"type": "record", "data": {"url": "https://x/a.silk"}},
            {"type": "video", "data": {"file": "https://x/a.mp4"}},
            {"type": "face", "data": {"id": 14}},
            {"type": "forward", "data": {"id": "fwd-1"}},
            {"type": "at", "data": {"qq": "all"}},
        ]
    )
    assert [s.kind for s in segs] == [
        SegmentKind.RECORD,
        SegmentKind.VIDEO,
        SegmentKind.FACE,
        SegmentKind.FORWARD,
        SegmentKind.AT_ALL,
    ]
    assert segs[0].data["url"] == "https://x/a.silk"
    assert segs[2].data["id"] == "14"
    assert segs[3].data["id"] == "fwd-1"


def test_segments_to_text_keeps_meaning() -> None:
    text = segments_to_text(
        [
            Segment.text("看这个："),
            Segment.markdown("**重点**"),
            Segment.record("x"),
            Segment.video("y"),
            Segment.forward([]),
        ]
    )
    assert text == "看这个：**重点**[语音][视频][合并转发]"


# --------------------------------------------------------------------------- #
# 合并转发
# --------------------------------------------------------------------------- #


def _onebot() -> Any:
    from haiqbot.adapters.onebot11 import OneBotAdapter

    return OneBotAdapter(OneBotCfg(), token_provider=lambda: None)


def _recording(adapter: Any) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def fake_call(action: str, params: dict, **_kw: Any) -> dict:
        calls.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"message_id": "fwd-9"}}

    adapter.call = fake_call  # type: ignore[method-assign]
    return calls


async def test_forward_uses_dedicated_action() -> None:
    adapter = _onebot()
    calls = _recording(adapter)
    nodes = [
        ForwardNode(user_id="111", name="小明", segments=(Segment.text("第一句"),)).to_dict(),
        ForwardNode(msg_id="999").to_dict(),
    ]
    assert await adapter.send_forward(GROUP, nodes) == "fwd-9"
    action, params = calls[0]
    assert action == "send_group_forward_msg"
    assert params["group_id"] == 674425800
    # 协议要求每个节点包一层 {"type": "node", "data": ...}
    assert params["messages"][0]["type"] == "node"
    assert params["messages"][1]["data"] == {"id": "999"}
    # 真机上就是这么踩坑的：节点 content 必须是 OneBot 的 {type,data}，
    # 不是我们内部的 {kind,data}，否则协议端只回 retcode 1200。
    assert params["messages"][0]["data"]["content"] == [{"type": "text", "data": {"text": "第一句"}}]


def test_forward_node_content_is_reencoded_to_onebot_shape() -> None:
    from haiqbot.adapters.onebot11 import _node_data

    node = ForwardNode(user_id="1", name="x", segments=(Segment.text("hi"), Segment.face(13))).to_dict()
    out = _node_data(node)
    assert out["content"] == [
        {"type": "text", "data": {"text": "hi"}},
        {"type": "face", "data": {"id": "13"}},
    ]
    # 引用型节点没有 content，原样保留
    assert _node_data({"id": "999"}) == {"id": "999"}


async def test_forward_private_channel() -> None:
    adapter = _onebot()
    calls = _recording(adapter)
    await adapter.send_forward(PRIVATE, [ForwardNode(msg_id="1").to_dict()])
    assert calls[0][0] == "send_private_forward_msg"
    assert calls[0][1]["user_id"] == 111


async def test_send_message_routes_forward_segment() -> None:
    """插件把合并转发当普通消息发时，也必须走专用 action，不能塞进 send_group_msg。"""
    adapter = _onebot()
    calls = _recording(adapter)
    await adapter.send_message(GROUP, [Segment.forward([ForwardNode(msg_id="1")])])
    actions = [c[0] for c in calls]
    assert actions == ["send_group_forward_msg"]


async def test_forward_requires_nodes() -> None:
    from haiqbot.core.errors import AdapterError

    adapter = _onebot()
    _recording(adapter)
    with pytest.raises(AdapterError):
        await adapter.send_forward(GROUP, [])


# --------------------------------------------------------------------------- #
# 官方 markdown + 按钮
# --------------------------------------------------------------------------- #


def test_official_markdown_body_shape() -> None:
    from haiqbot.adapters import official as mod

    payloads: list[dict] = []

    class FakeOfficial(mod.OfficialAdapter):
        capabilities = PlatformCapabilities(markdown=True, button=True)

        def __init__(self) -> None:  # 不连网
            self.platform = "official"

        async def _send_payload(self, channel: Any, payload: dict, *, reply_to: Any, seq: Any) -> str:
            payloads.append(payload)
            return "m1"

    fake = FakeOfficial()
    rows = [{"buttons": [{"id": "1", "render_data": {"label": "点我"}}]}]
    import asyncio

    asyncio.run(
        fake._raw_send(
            ChannelId("official", ChannelKind.GROUP, "g1"),
            [Segment.markdown("**你好**"), Segment.keyboard(rows)],
        )
    )
    body = payloads[0]
    assert body["msg_type"] == mod.MSG_MARKDOWN
    assert body["markdown"] == {"content": "**你好**"}
    # 内核只套外壳，按钮内层结构原样透传
    assert body["keyboard"] == {"content": {"rows": rows}}
    assert body["content"], "官方要求 content 非空"


def _fake_official(payloads: list[dict], uploads: list[tuple[str, dict]]) -> Any:
    from haiqbot.adapters import official as mod

    class Fake(mod.OfficialAdapter):
        capabilities = PlatformCapabilities(markdown=True, button=True, record=True, video=True)

        def __init__(self) -> None:
            self.platform = "official"

        async def _send_payload(self, channel: Any, payload: dict, *, reply_to: Any, seq: Any) -> str:
            payloads.append(payload)
            return "m1"

        async def _send_media(
            self, channel: Any, url: str, *, file_type: int = 1, reply_to: Any, seq: Any
        ) -> str:
            uploads.append((url, {"file_type": file_type}))
            return "m2"

    return Fake()


def test_official_media_file_types() -> None:
    """官方富媒体靠 /files 的 file_type 区分：1 图片 / 2 视频 / 3 语音。"""
    from haiqbot.adapters.official import FILE_TYPE_AUDIO, FILE_TYPE_IMAGE, FILE_TYPE_VIDEO

    payloads: list[dict] = []
    uploads: list[tuple[str, dict]] = []
    adapter = _fake_official(payloads, uploads)
    import asyncio

    asyncio.run(
        adapter._raw_send(
            ChannelId("official", ChannelKind.GROUP, "g1"),
            [
                Segment.image("https://x/a.png"),
                Segment.video("https://x/a.mp4"),
                Segment.record("https://x/a.silk"),
            ],
        )
    )
    assert [u[1]["file_type"] for u in uploads] == [
        FILE_TYPE_IMAGE,
        FILE_TYPE_VIDEO,
        FILE_TYPE_AUDIO,
    ]
    assert uploads[2][0] == "https://x/a.silk"


def test_official_media_without_url_is_reported_not_skipped() -> None:
    """只有 path 没有 url：官方 /files 的 url 必填 → 必须报错，不能静默丢图。

    （旧行为是 log.warning + continue，群里看起来「发成功了」，实际图没进群。）
    """
    import asyncio

    import pytest

    from haiqbot.core.errors import MediaSendError

    payloads: list[dict] = []
    uploads: list[tuple[str, dict]] = []
    adapter = _fake_official(payloads, uploads)

    with pytest.raises(MediaSendError):
        asyncio.run(
            adapter._raw_send(
                ChannelId("official", ChannelKind.GROUP, "g1"),
                [Segment.record(path="/tmp/a.silk")],
            )
        )
    assert uploads == []


# --------------------------------------------------------------------------- #
# Reply 降级
# --------------------------------------------------------------------------- #


async def test_reply_degrades_when_platform_lacks_media() -> None:
    adapter = MockAdapter()
    adapter.capabilities = PlatformCapabilities(record=False, video=False, forward=False)
    from haiqbot.core.reply import Reply

    reply = Reply(adapter, GROUP)
    await reply.record("https://x/a.silk")
    await reply.video("https://x/a.mp4")
    await reply.forward([ForwardNode(segments=(Segment.text("被折叠的内容"),))])
    texts = adapter.texts()
    assert any("不支持发送语音" in t for t in texts)
    assert any("不支持发送视频" in t for t in texts)
    assert any("被折叠的内容" in t for t in texts), "合并转发降级也要把内容露出来"
