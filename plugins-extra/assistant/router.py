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

#: 我们**主动限定**的档位，吸附只会落到这里。
#:
#: 这不是"接口只认这些"。实测（2026-10-24，gpt-image-2 网关）：
#:   - 1024x768(4:3)、3840x2160(4K) 这类非预设值同样返回 200，不是 400；
#:   - 网关会按**像素量**归一化：低于约 1.57MP 的请求被等比放大到该量级
#:     （1024x1024 -> 1254x1254，1024x768 -> 1448x1086，比例保持不变），
#:     高于它的原样返回（1920x1080 / 4K 均为精确尺寸）。
#: 仍然白名单化的三条理由，都与"接口限制"无关：
#:   ① 比例可预期 —— 用户要什么比例，出图就是什么比例；
#:   ② 体积/耗时可控 —— 4K 出 12.6MB，base64 后约 16.8MB，发群风险很大；
#:   ③ 比例/方向词必须翻译成"接口认得的字符串"，这份表就是翻译目标。
SIZES: dict[str, tuple[int, int]] = {
    "1024x1024": (1024, 1024),
    "1536x1024": (1536, 1024),
    "1024x1536": (1024, 1536),
    # 16:9 两档：gpt-image-2 实测接受并原样返回（1920x1080 / 1080x1920）。
    # 加它们之前 16:9 只能被吸附到 1.5 的 1536x1024，用户要"电脑壁纸"会拿到偏方的图。
    "1920x1080": (1920, 1080),
    "1080x1920": (1080, 1920),
    "512x512": (512, 512),
}
DEFAULT_SIZE = "1024x1024"

# 1920x1080 / 1920*1080 / 1920×1080 / 1920 X 1080
_RES_RE = re.compile(r"(\d{2,5})\s*[x×*X]\s*(\d{2,5})")
# 16:9 / 16：9（全角冒号）/ 16比9 / 16/9
_RATIO_RE = re.compile(r"(\d{1,2})\s*[:：比/]\s*(\d{1,2})")
# 长1920 宽1080 / 宽 1080 高 1920（数字必须紧邻，否则"长发"会被当成"长=发"）
_DIM_RE = re.compile(r"(长|宽|高)[度]?\s*[:：]?\s*(\d{2,5})")

#: 方向词 → 宽高比。**只做枚举翻译，不做语言理解。**
#:
#: 这些是模型契约里允许返回的 size_hint（见 SYSTEM_PROMPT）。接口并不认识这些字符串
#: （实测：传 "landscape" 网关会拿它自己的横图 3:2，而不是 16:9），所以必须换成像素。
#:
#: 用户口语里的"电脑壁纸 / 手机壁纸 / 横版壁纸"**故意不在这里**：那是语义理解，归模型。
#: 实测模型对这类说法 6/6 都能给出 landscape/portrait；正则再猜一遍等于维护两套口径，
#: 而两套口径一旦不一致，模型说对了也会被正则改错。
_WORD_RATIOS = {
    "landscape": 16 / 9,
    "portrait": 9 / 16,
    "square": 1.0,
    "横版": 16 / 9,
    "横屏": 16 / 9,
    "竖版": 9 / 16,
    "竖屏": 9 / 16,
    "方图": 1.0,
    "正方形": 1.0,
}
#: pc / PC / Landscape 都要认，所以不分大小写。
_WORD_RE = re.compile("|".join(sorted(_WORD_RATIOS, key=len, reverse=True)), re.IGNORECASE)
#: 正则不分大小写，回查也得不分大小写 —— 否则 "PC壁纸" 命中却查不到键。
_WORD_LOOKUP = {key.lower(): value for key, value in _WORD_RATIOS.items()}

#: 中文里"宽"指水平、"长/高"指竖直 —— 这是本模块唯一的语义假设，写在这里备查。
_HORIZONTAL = "宽"
_VERTICAL = ("长", "高")


@dataclass(frozen=True)
class SizeHint:
    """用户表达的尺寸。`spans` 是"要从 prompt 里剥掉"的区间。"""

    label: str
    ratio: float
    spans: tuple[tuple[int, int], ...] = field(default_factory=tuple)
    #: 命中类型：res=显式分辨率｜dim=宽高标注｜ratio=比例｜word=方向词
    kind: str = "word"
    width: int | None = None
    height: int | None = None


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
    kind = "word"
    px_w: int | None = None
    px_h: int | None = None

    # ① 显式分辨率（最准）
    m = _RES_RE.search(text)
    if m:
        width, height = int(m.group(1)), int(m.group(2))
        if width >= 64 and height >= 64:
            ratio = width / height
            label = f"{width}x{height}"
            kind = "res"
            px_w, px_h = width, height
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
            kind = "dim"
            px_w, px_h = horizontal, vertical
            spans.extend(dim_spans.values())

    # ③ 比例
    if ratio is None:
        rm = _RATIO_RE.search(text)
        if rm:
            a, b = int(rm.group(1)), int(rm.group(2))
            if a > 0 and b > 0:
                ratio = a / b
                label = f"{a}:{b}"
                kind = "ratio"
                spans.append(rm.span())

    # ④ 形容词
    if ratio is None:
        wm = _WORD_RE.search(text)
        if wm:
            ratio = _WORD_LOOKUP[wm.group(0).lower()]
            label = wm.group(0)
            spans.append(wm.span())

    if ratio is None:
        return None

    # 顺带把同句里多余的尺寸写法也剥掉，避免 "16:9 1920x1080 的猫" 残留
    for rx in (_RES_RE, _DIM_RE, _RATIO_RE, _WORD_RE):
        for extra in rx.finditer(text):
            if extra.span() not in spans:
                spans.append(extra.span())
    return SizeHint(
        label=label,
        ratio=ratio,
        spans=tuple(sorted(spans)),
        kind=kind,
        width=px_w,
        height=px_h,
    )


#: 体积闸门：显式分辨率超过这个像素量就不再透传，改为吸附到最近的档位。
#: 依据：1920x1080 出 4.2MB（base64 5.6MB，发送约 4s）；4K 出 12.6MB
#: （base64 约 16.8MB），发群风险明显上升。
MAX_PIXELS = 1920 * 1080


def absorb_size(ratio: float) -> str:
    """把任意宽高比吸附到最接近的合法档位。

    用**对数距离**而不是差值：1.78 与 1.50 的差距，和 0.56 与 0.67 的差距，
    在比例空间里才是可比的（差值会把竖图判错）。
    """
    if not ratio or ratio <= 0:
        return DEFAULT_SIZE
    return min(SIZES, key=lambda key: abs(math.log((SIZES[key][0] / SIZES[key][1]) / ratio)))


def _finalize(parsed: SizeHint) -> str:
    """把一次解析结果落成接口接受的参数。

    显式分辨率（`1920x1080`、`宽1920 高1080`）**原样透传**：用户写的数字就是他的意图，
    不该被静默改成别的档位（实测接口对非预设值同样返回 200，并精确返回所请求的尺寸）。
    比例 / 方向词（`16:9`、`landscape`）吸附到档位：接口不认这些字符串。
    唯一例外：显式分辨率超过 `MAX_PIXELS` 时压回档位，避免生成十几 MB 的巨图。
    """
    if parsed.kind in ("res", "dim") and parsed.width and parsed.height:
        if parsed.width * parsed.height <= MAX_PIXELS:
            return f"{parsed.width}x{parsed.height}"
    return absorb_size(parsed.ratio)


def decide_size(
    original: str, hint: str | None = None, *, default: str = DEFAULT_SIZE
) -> tuple[str, SizeHint | None]:
    """决定最终档位，返回 (档位, 命中的尺寸信息)。

    优先级：**模型给的 size_hint > 用户原句里的兜底线索 > 默认档**。

    尺寸判断完全交给模型（用户明确要求）：模型能听懂"电脑壁纸就是横的""做个能当
    手机锁屏的""竖着的那种"这类说法，正则词表做不到。
    代码只保留两道保险，都不改变模型的决定：
      1. 模型没给 size_hint（null）时，才用正则从原句兜底 —— 否则会退回方图；
      2. 任何结果都经 `absorb_size` 吸附到接口认得的档位 —— 绝不透传，透传会 400。
    """
    for candidate in (hint or "", original or ""):
        parsed = parse_size(candidate)
        if parsed is not None:
            return _finalize(parsed), parsed
    if default in SIZES or default == "auto":
        return default, None
    return DEFAULT_SIZE, None


def describe_size(key: str) -> str:
    """`1536x1024` -> `1536×1024`（给人看）。也支持透传的任意分辨率。"""
    size = SIZES.get(key) or _parse_key(key)
    return f"{size[0]}×{size[1]}" if size else key


def _parse_key(key: str) -> tuple[int, int] | None:
    """从 `1280x720` 这种参数里读回宽高（不在 SIZES 里的透传值）。"""
    m = re.fullmatch(r"(\d{2,5})x(\d{2,5})", (key or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


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
SYSTEM_PROMPT = """你是蔡小姬，群聊里的活泼姑娘。判断用户这句话是不是"请我生成一张图片"，并给出回复。

只输出一个 JSON 对象，不要 markdown 围栏，不要解释。字段：
{
  "action": "image" 或 "text",
  "prompt": "画图用的纯视觉描述；action=text 时为 null",
  "size_hint": "用户提到的比例或分辨率原文（如 16:9 / 1920x1080）；只表达了横/竖/方就用 landscape / portrait / square；都没提就 null",
  "reply": "给群友看的一句话",
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
4. action=text 时 reply 就是蔡小姬的回答：口语、像在群里聊天，一般 1~3 句、
   不超过 150 字。不要客服腔，不要分点罗列（对方真要步骤时除外）。
5. action=image 时 reply 是蔡小姬把这个活接下来的一句话：活泼、俏皮，一两句就行，
   可以点出画面里的一个具体细节 —— 比如"橘猫是吧，帽檐压那么低装深沉呢"。
   - 写成陈述或感叹，别用问句收尾，也别问用户要风格 / 比例 / 分辨率 ——
     你已经在画了，先把活接住，剩下的自己判断。
6. confidence 表示你对 action 判断的把握。拿不准就给 0.5 左右。
7. violation.hit 只在用户这句话本身含广告引流、辱骂、色情、违法内容时为 true。
   正常请求（包括画图）一律 false。
8. 用户消息可能以「昵称: 」开头，那只是发言人的标记（可能是群昵称、群名或备注）：
   直接用它称呼对方就行，不要复述、不要解释、不要说你不知道它是什么。"""

#: 不放 few-shot 示范。
#:
#: 2026-10-25 实测（deepseek-v4.1-flash，10 句边界用例 × 2 轮 + 6 句 image 用例 × 3 轮）：
#:   - 判定准确率：带满 6 条示范 100%，一条不留也是 100% —— 分类不靠示范；
#:   - 唯一差异是 image 的 reply 长度（全删后 7/18 超过 30 字，均值 19.6 → 29.4 字）。
#: 「reply 长几句」不影响正确性，所以把 12 条 few-shot 消息整个去掉：
#: 每次调用省约 800 input token，也少一份要跟着 SYSTEM_PROMPT 同步维护的文本。
FEW_SHOT: tuple[tuple[str, str], ...] = ()


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
    """组装发给模型的完整消息（system + 历史 + 本次提问）。

    这里曾经拼进 6 组 few-shot。2026-10-25 的 A/B 实测表明它换不来任何判定收益，
    只让 reply 短几字，于是整段去掉 —— 见文件上方 FEW_SHOT 处的原因记录。
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
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
