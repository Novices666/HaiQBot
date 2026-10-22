"""消息段：跨协议的统一消息表达。

一个 `Segment` 只描述「这是什么、指向什么」，不关心怎么发出去。
编码/解码由各 adapter 负责。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SegmentKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AT = "at"
    AT_ALL = "at_all"
    REPLY = "reply"
    FILE = "file"
    FACE = "face"
    RECORD = "record"
    VIDEO = "video"
    MARKDOWN = "markdown"
    KEYBOARD = "keyboard"
    FORWARD = "forward"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Segment:
    """一段消息。

    data 的约定键：
    - text:   {"text": "..."}
    - image:  {"url": ...} | {"media_id": ...} | {"path": ...}
    - at:     {"user_id": "..."}  (user_id == "all" 等价于 AT_ALL)
    - reply:  {"msg_id": "..."}
    - file:   {"url": ...} | {"path": ..., "name": ...}
    - record: {"url": ...} | {"path": ...}         语音（OneBot 侧多为 silk/amr）
    - video:  {"url": ...} | {"path": ...}
    - face:   {"id": "..."}                        平台表情 ID（QQ 的 face id）
    - markdown: {"text": "..."}                    QQ 官方的 markdown 正文
    - keyboard: {"rows": [...]}                    **原样透传**给平台的按钮结构
    - forward: {"nodes": [{...}, ...]}             合并转发
    """

    kind: SegmentKind
    data: dict[str, Any] = field(default_factory=dict)

    # -- 构造糖 ----------------------------------------------------------- #
    @staticmethod
    def text(value: str) -> Segment:
        return Segment(SegmentKind.TEXT, {"text": value})

    @staticmethod
    def image(url: str | None = None, *, media_id: str | None = None, path: str | None = None) -> Segment:
        data: dict[str, Any] = {}
        if url:
            data["url"] = url
        if media_id:
            data["media_id"] = media_id
        if path:
            data["path"] = path
        return Segment(SegmentKind.IMAGE, data)

    @staticmethod
    def at(user_id: str) -> Segment:
        return Segment(SegmentKind.AT_ALL if user_id == "all" else SegmentKind.AT, {"user_id": user_id})

    @staticmethod
    def reply(msg_id: str) -> Segment:
        return Segment(SegmentKind.REPLY, {"msg_id": msg_id})

    @staticmethod
    def file(url: str | None = None, *, path: str | None = None, name: str | None = None) -> Segment:
        data: dict[str, Any] = {}
        if url:
            data["url"] = url
        if path:
            data["path"] = path
        if name:
            data["name"] = name
        return Segment(SegmentKind.FILE, data)

    @staticmethod
    def record(url: str | None = None, *, path: str | None = None) -> Segment:
        data: dict[str, Any] = {}
        if url:
            data["url"] = url
        if path:
            data["path"] = path
        return Segment(SegmentKind.RECORD, data)

    @staticmethod
    def video(url: str | None = None, *, path: str | None = None) -> Segment:
        data: dict[str, Any] = {}
        if url:
            data["url"] = url
        if path:
            data["path"] = path
        return Segment(SegmentKind.VIDEO, data)

    @staticmethod
    def face(face_id: str | int) -> Segment:
        return Segment(SegmentKind.FACE, {"id": str(face_id)})

    @staticmethod
    def markdown(text: str) -> Segment:
        """QQ 官方的 markdown 正文。其他平台会退化成纯文本。"""
        return Segment(SegmentKind.MARKDOWN, {"text": text})

    @staticmethod
    def keyboard(rows: list[dict[str, Any]]) -> Segment:
        """按钮行。

        **内核不定义按钮 schema** —— QQ 官方的 keyboard 结构（render_data /
        action / permission …）由平台演进，抄一份到内核里必然过期。
        这里只要求一个可 JSON 序列化的 rows 列表，编码时原样透传。
        """
        return Segment(SegmentKind.KEYBOARD, {"rows": rows})

    @staticmethod
    def forward(nodes: list[ForwardNode]) -> Segment:
        return Segment(SegmentKind.FORWARD, {"nodes": [n.to_dict() for n in nodes]})

    # -- 序列化 ----------------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {"kind": str(self.kind), "data": dict(self.data)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Segment:
        return cls(SegmentKind(payload.get("kind", "unknown")), dict(payload.get("data") or {}))

    def __str__(self) -> str:
        if self.kind is SegmentKind.TEXT:
            return str(self.data.get("text", ""))
        if self.kind is SegmentKind.AT:
            return f"@{self.data.get('user_id', '')}"
        return f"[{self.kind}]"


@dataclass(frozen=True, slots=True)
class ForwardNode:
    """合并转发里的一条子消息。

    两种形态：
    - 自建节点：`segments` + `user_id` + `name`
    - 引用已有消息：`msg_id`（平台侧把那条消息原样塞进转发）
    """

    segments: tuple[Segment, ...] = ()
    user_id: str = ""
    name: str = ""
    msg_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if self.msg_id:
            data["id"] = self.msg_id
        if self.segments:
            data["content"] = [dict(kind=str(x.kind), data=dict(x.data)) for x in self.segments]
        if self.user_id:
            data["user_id"] = self.user_id
        if self.name:
            data["nickname"] = self.name
        return data


def segments_to_text(segments: list[Segment]) -> str:
    """抽出文本部分，用于命令解析。

    markdown 也算文本（命令不该因为消息是 markdown 就失灵）；
    媒体段给一个占位符，这样"这条消息里有什么"在日志里还看得见。
    """
    out: list[str] = []
    for s in segments:
        if s.kind is SegmentKind.TEXT or s.kind is SegmentKind.MARKDOWN:
            out.append(str(s.data.get("text", "")))
        elif s.kind is SegmentKind.RECORD:
            out.append("[语音]")
        elif s.kind is SegmentKind.VIDEO:
            out.append("[视频]")
        elif s.kind is SegmentKind.FORWARD:
            out.append("[合并转发]")
        elif s.kind is SegmentKind.UNKNOWN:
            out.append(_unknown_label(s))
    return "".join(out)


# 未知段的文案：优先抽可见文字（卡片正文常在 prompt/title/content 里），
# 抽不到才给占位符 —— 至少让插件知道"这条消息里有东西"。
_UNKNOWN_TEXT_KEYS = ("prompt", "title", "content", "text", "desc", "summary")


def _unknown_label(seg: Segment) -> str:
    for key in _UNKNOWN_TEXT_KEYS:
        value = seg.data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raw_type = str(seg.data.get("raw_type") or "unknown")
    return _UNKNOWN_LABELS.get(raw_type, f"[{raw_type}]")


_UNKNOWN_LABELS = {
    "json": "[卡片]",
    "xml": "[卡片]",
    "mface": "[表情]",
    "share": "[链接]",
    "location": "[位置]",
    "contact": "[名片]",
    "music": "[音乐]",
    "rps": "[猜拳]",
    "dice": "[骰子]",
    "shake": "[窗口抖动]",
    "poke": "[戳一戳]",
    "anonymous": "[匿名]",
    "node": "[转发节点]",
    "file": "[文件]",
}


def has_at_bot(segments: list[Segment], bot_id: str | None) -> bool:
    if bot_id is None:
        return False
    return any(s.kind is SegmentKind.AT and str(s.data.get("user_id")) == str(bot_id) for s in segments)
