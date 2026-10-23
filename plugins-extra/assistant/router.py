"""意图路由 + 尺寸解析。

两件事都做成**纯函数**（不碰网络、不碰 ctx），因为它们直接决定"这次要不要烧生图
额度"，必须能被单元测试一条条钉住，也必须能在日志里解释清楚。

判断为什么交给模型：关键词只能覆盖"有没有生成动作"，覆盖不了"产物是不是视觉物"
（生成 UUID）和"是请求还是讨论"（能不能画图？）。这两件事需要语义。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# 尺寸：解析
# --------------------------------------------------------------------------- #

#: 接口真正认的档位。吸附只会落到这里，绝不把用户的任意数字透传下去
#: （透传的结果是 provider 返回 400，用户只看到"生成失败"）。
SIZES: dict[str, tuple[int, int]] = {
    "1024x1024": (1024, 1024),
    "1536x1024": (1536, 1024),
    "1024x1536": (1024, 1536),
    "512x512": (512, 512),
}
DEFAULT_SIZE = "1024x1024"

# 1920x1080 / 1920*1080 / 1920×1080 / 1920 X 1080
_RES_RE = re.compile(r"(\d{2,5})\s*[x×*X]\s*(\d{2,5})")
# 16:9 / 16：9（全角冒号）/ 16比9 / 16/9
_RATIO_RE = re.compile(r"(\d{1,2})\s*[:：比/]\s*(\d{1,2})")
# 长1920 宽1080 / 宽 1080 高 1920（数字必须紧邻，否则"长发"会被当成"长=发"）
_DIM_RE = re.compile(r"(长|宽|高)[度]?\s*[:：]?\s*(\d{2,5})")

#: 形容词 → 宽高比。只认这些明确的词，不猜"大一点""高清"。
_WORD_RATIOS = {
    "手机壁纸": 9 / 16,
    "竖版": 9 / 16,
    "竖屏": 9 / 16,
    "桌面壁纸": 3 / 2,
    "横版": 3 / 2,
    "横屏": 3 / 2,
    "宽屏": 3 / 2,
    "方图": 1.0,
    "正方形": 1.0,
}
_WORD_RE = re.compile("|".join(sorted(_WORD_RATIOS, key=len, reverse=True)))

#: 中文里"宽"指水平、"长/高"指竖直 —— 这是本模块唯一的语义假设，写在这里备查。
_HORIZONTAL = "宽"
_VERTICAL = ("长", "高")


@dataclass(frozen=True)
class SizeHint:
    """用户表达的尺寸。`spans` 是"要从 prompt 里剥掉"的区间。"""

    label: str
    ratio: float
    spans: tuple[tuple[int, int], ...] = field(default_factory=tuple)


def parse_size(raw: str) -> SizeHint | None:
    """从原句里抽尺寸。优先级：显式分辨率 > 长宽高 > 比例 > 形容词。

    没有任何尺寸信息就返回 None（调用方用配置的默认档）。
    """
    text = raw or ""
    if not text.strip():
        return None

    spans: list[tuple[int, int]] = []
    ratio: float | None = None
    label = ""

    # ① 显式分辨率（最准）
    m = _RES_RE.search(text)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
        if width >= 64 and height >= 64:
            ratio = width / height
            label = f"{width}x{height}"
            spans.append(m.span())

    # ② 长/宽/高 + 数字（两个维度才够）
    if ratio is None:
        dims: dict[str, int] = {}
        dim_spans: dict[str, tuple[int, int]] = {}
        for dm in _DIM_RE.finditer(text):
            dims[dm.group(1)] = int(dm.group(2))
            dim_spans[dm.group(1)] = dm.span()
        horizontal = dims.get(_HORIZONTAL)
        vertical = next((dims[k] for k in _VERTICAL if k in dims), None)
        if horizontal and vertical and horizontal >= 64 and vertical >= 64:
            ratio = horizontal / vertical
            label = f"宽{horizontal} 高{vertical}"
            spans.extend(dim_spans.values())

    # ③ 比例
    if ratio is None:
        rm = _RATIO_RE.search(text)
        if rm:
            a, b = int(rm.group(1)), int(rm.group(2))
            if a > 0 and b > 0:
                ratio = a / b
                label = f"{a}:{b}"
                spans.append(rm.span())

    # ④ 形容词
    if ratio is None:
        wm = _WORD_RE.search(text)
        if wm:
            ratio = _WORD_RATIOS[wm.group(0)]
            label = wm.group(0)
            spans.append(wm.span())

    if ratio is None:
        return None

    # 顺带把同句里多余的尺寸写法也剥掉，避免 "16:9 1920x1080 的猫" 残留
    for rx in (_RES_RE, _DIM_RE, _RATIO_RE, _WORD_RE):
        for extra in rx.finditer(text):
            if extra.span() not in spans:
                spans.append(extra.span())
    return SizeHint(label=label, ratio=ratio, spans=tuple(sorted(spans)))


def absorb_size(ratio: float) -> str:
    """把任意宽高比吸附到最接近的合法档位。

    用**对数距离**而不是差值：1.78 与 1.50 的差距，和 0.56 与 0.67 的差距，
    在比例空间里才是可比的（差值会把竖图判错）。
    """
    if not ratio or ratio <= 0:
        return DEFAULT_SIZE
    return min(SIZES, key=lambda key: abs(math.log((SIZES[key][0] / SIZES[key][1]) / ratio)))


def describe_size(key: str) -> str:
    """`1536x1024` -> `1536×1024`（给人看）。"""
    width, height = SIZES.get(key, (0, 0))
    return f"{width}×{height}" if width else key


def strip_spans(text: str, spans: tuple[tuple[int, int], ...]) -> str:
    """把尺寸片段从描述里删掉 —— 否则图里会出现"16:9"这几个字。"""
    if not spans:
        return text.strip()
    out: list[str] = []
    cursor = 0
    for start, end in spans:
        if start > cursor:
            out.append(text[cursor:start])
        cursor = max(cursor, end)
    out.append(text[cursor:])
    cleaned = "".join(out)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" ，,、。;；:：的")


# --------------------------------------------------------------------------- #
# 意图：模型契约
# --------------------------------------------------------------------------- #

#: 模型必须返回的 JSON 结构。字段少而硬，便于校验。
SYSTEM_PROMPT = """你是群聊助手。判断用户这句话是不是"请我生成一张图片"，并给出回复。

只输出一个 JSON 对象，不要 markdown 围栏，不要解释。字段：
{
  "action": "image" 或 "text",
  "reply": "给群友看的一句话",
  "prompt": "画图用的纯视觉描述；action=text 时为 null",
  "size_hint": "用户提到的比例或分辨率原文（如 16:9 / 1920x1080），没提就 null",
  "confidence": 0 到 1 的小数,
  "why": "一句话理由",
  "violation": {"hit": true/false, "category": "广告/引流/辱骂/色情/违法/其他", "why": "一句话"}
}

判断规则（按顺序）：
1. 只有用户**明确请求生成一张图**时才 action=image。例："帮我画一只猫"、"生成一张赛博朋克城市夜景"、"来张 16:9 的壁纸"。
2. 以下一律 action=text：讨论或询问能力（"能不能画图？"、"你会画画吗"）、
   成语或比喻（"画个饼"、"draw a conclusion"）、产物不是图（"生成一个 UUID"、
   "生成一段 SQL"、"生成报表"）、在描述或评价已有图片（"这张图片里的猫好可爱"）。
3. action=image 时 prompt 必须是**纯视觉描述**：去掉"请/帮我/画/生成/谢谢"等
   关照与动作词，去掉尺寸词（尺寸走 size_hint），不要出现文字排版要求以外的指令。
4. action=text 时 reply 就是正式回答，简洁、中文、不超过 150 字。
5. action=image 时 reply 是一句简短预告，例如"好的，我来画一只戴帽子的小猫"。
6. confidence 表示你对 action 判断的把握。拿不准就给 0.5 左右。
7. violation.hit 只在用户这句话本身含广告引流、辱骂、色情、违法内容时为 true。
   正常请求（包括画图）一律 false。"""

#: few-shot：覆盖最容易误判的几类。多语言各给一条。
FEW_SHOT: tuple[tuple[str, str], ...] = (
    (
        "帮我画一张小猫图吧",
        '{"action":"image","reply":"好的，我来画一只小猫","prompt":"一只可爱的小猫，柔和自然光，特写，干净背景",'
        '"size_hint":null,"confidence":0.96,"why":"明确的画图请求","violation":{"hit":false,"category":"","why":""}}',
    ),
    (
        "帮我生成一个 UUID",
        '{"action":"text","reply":"生成 UUID 用 Python 一句话就行：import uuid; print(uuid.uuid4())",'
        '"prompt":null,"size_hint":null,"confidence":0.94,"why":"产物是字符串不是图","violation":{"hit":false,"category":"","why":""}}',
    ),
    (
        "draw a conclusion from this table",
        '{"action":"text","reply":"这句话是英语惯用语，意思是「得出结论」，不是要我画画。",'
        '"prompt":null,"size_hint":null,"confidence":0.9,"why":"draw a conclusion 是惯用搭配","violation":{"hit":false,"category":"","why":""}}',
    ),
    (
        "能不能画图？",
        '{"action":"text","reply":"可以。想要图就说「画一张……」或者直接 /画图 <描述>。",'
        '"prompt":null,"size_hint":null,"confidence":0.88,"why":"在问能力，不是下请求","violation":{"hit":false,"category":"","why":""}}',
    ),
    (
        "画个饼给我看看",
        '{"action":"text","reply":"哈哈，「画个饼」我就不画了，真想要图的话说「画一张饼」试试。",'
        '"prompt":null,"size_hint":null,"confidence":0.8,"why":"成语/调侃","violation":{"hit":false,"category":"","why":""}}',
    ),
    (
        "画个 16:9 的赛博朋克城市夜景",
        '{"action":"image","reply":"收到，正在画 16:9 的赛博朋克城市夜景",'
        '"prompt":"赛博朋克风格的城市夜景，霓虹灯，雨夜街道，电影感",'
        '"size_hint":"16:9","confidence":0.95,"why":"明确请求且给了比例","violation":{"hit":false,"category":"","why":""}}',
    ),
)


@dataclass(frozen=True)
class Violation:
    category: str = ""
    why: str = ""


@dataclass(frozen=True)
class Decision:
    action: str
    reply: str
    prompt: str | None = None
    size_hint: str | None = None
    confidence: float = 0.0
    why: str = ""
    violation: Violation | None = None

    @property
    def wants_image(self) -> bool:
        return self.action == "image" and bool((self.prompt or "").strip())


def build_messages(
    text: str,
    history: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """组装发给模型的完整消息（system + few-shot + 历史 + 本次提问）。"""
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for user, assistant in FEW_SHOT:
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": assistant})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})
    return messages


def _extract_json(raw: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 围栏与前后废话）。"""
    text = (raw or "").strip()
    if not text:
        return None
    for candidate in (text, text.strip("`").removeprefix("json").strip()):
        try:
            loaded = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(loaded, dict):
            return loaded

    start = text.find("{")
    if start < 0:
        return None
    end = text.rfind("}")
    while end > start:
        try:
            loaded = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            end = text.rfind("}", start, end)
            continue
        return loaded if isinstance(loaded, dict) else None
    return None


def parse_decision(raw: str) -> Decision | None:
    """把模型输出变成 `Decision`。解析不出来返回 None —— 调用方降级为纯文本回答。

    **校验失败绝不出图**：宁可少画一张，也不要在看不懂的返回上烧额度。
    """
    data = _extract_json(raw)
    if not data:
        return None

    action = str(data.get("action") or "").strip().lower()
    if action not in ("image", "text"):
        return None

    reply = str(data.get("reply") or "").strip()
    prompt = data.get("prompt")
    prompt_text = str(prompt).strip() if isinstance(prompt, str) else None
    if action == "image" and not prompt_text:
        return None  # 说要画但没给描述 → 不可执行

    size_hint = data.get("size_hint")
    hint = str(size_hint).strip() if isinstance(size_hint, str) and size_hint.strip() else None

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    violation: Violation | None = None
    raw_violation = data.get("violation")
    if isinstance(raw_violation, dict) and raw_violation.get("hit"):
        violation = Violation(
            category=str(raw_violation.get("category") or "其他").strip(),
            why=str(raw_violation.get("why") or "").strip(),
        )

    return Decision(
        action=action,
        reply=reply or ("🎨 收到，正在画…" if action == "image" else ""),
        prompt=prompt_text,
        size_hint=hint,
        confidence=confidence,
        why=str(data.get("why") or "").strip(),
        violation=violation,
    )
