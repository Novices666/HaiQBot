"""内容审查：词表先拦（免费、确定），未命中再问模型。

为什么不是纯模型：每条消息都过一次模型，成本翻倍、延迟翻倍，而且判定结果不可复现
（今天拦明天不拦）。为什么不是纯词表：词表绕得过，也不懂上下文（"我要杀了这个 bug"
不是威胁）。

所以：词表负责**确定性的硬违规**，模型负责**语义判断**。两边都命中就按先到的算。

本模块是纯函数，只有 `build_check_messages` 产出的内容会发到网络。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

#: 占位词表 —— 换成你自己群的真规则。故意写成"广告/引流"这类通用项，
#: 避免在这里塞进任何具体群的黑名单（那是隐私，应该放配置里）。
DEFAULT_WORDS: tuple[str, ...] = (
    # 广告 / 引流
    "加微信",
    "加我微信",
    "私聊我",
    "扫码进群",
    "刷单",
    "代开发票",
    "博彩",
    "棋牌室",
    "一夜暴富",
    "兼职日结",
    # 违法
    "办证",
    "枪支",
    "毒品",
    "冰毒",
)

#: 结构性违规（用词表表达不了的）。刻意收得很窄：宁可漏，不可误伤。
DEFAULT_PATTERNS: tuple[str, ...] = (
    r"(?<!\d)1[3-9]\d{9}(?!\d)",  # 手机号（前后不能紧邻数字）
    r"加\s*[Vv]\s*[:：]",  # "加V："
)

CATEGORIES = ("广告引流", "辱骂", "色情", "违法", "其他")

CHECK_PROMPT = """你是群聊内容审查员。判断下面这条消息是否应当被拦截。

只输出一个 JSON 对象，不要 markdown 围栏：
{"hit": true 或 false, "category": "广告引流/辱骂/色情/违法/其他", "why": "一句话理由"}

判定标准：
- 广告引流：推销、拉人、导流到站外（私聊、加好友、进群）。
- 辱骂：针对具体人的侮辱、人身攻击。
- 色情：露骨性内容。
- 违法：违禁交易、诈骗、涉毒涉枪等。
- 正常聊天、玩笑、技术讨论（哪怕措辞激烈，如"这 bug 快把我搞死了"）、
  求解、请教、提问 一律 hit=false。
- 拿不准就 hit=false：误拦正常群友比漏拦一条更糟。"""


@dataclass(frozen=True)
class Hit:
    """一次命中。`evidence` 会进日志和面板，所以只放命中的词/类别，不放整句。"""

    category: str
    evidence: str = ""


def words_from_config(value: object) -> tuple[str, ...]:
    """把配置里的词表（逗号/换行分隔的字符串，或列表）解析成元组。"""
    if isinstance(value, (list, tuple)):
        items = [str(x) for x in value]
    else:
        items = re.split(r"[,，\n;；]", str(value or ""))
    return tuple(w.strip() for w in items if w.strip())


def lexicon_hit(
    text: str,
    words: tuple[str, ...] = (),
    patterns: tuple[str, ...] = (),
) -> Hit | None:
    """词表 / 正则拦截。命中返回 `Hit`，否则 None。"""
    body = text or ""
    if not body.strip():
        return None
    lowered = body.lower()
    for word in words:
        if word and word.lower() in lowered:
            return Hit(category="词表", evidence=word)
    for pattern in patterns:
        try:
            if re.search(pattern, body):
                return Hit(category="正则", evidence=pattern)
        except re.error:  # 配置里写坏了不该让插件崩
            continue
    return None


def build_check_messages(text: str) -> list[dict[str, str]]:
    """组装"这条消息该不该拦"的判定请求。"""
    return [
        {"role": "system", "content": CHECK_PROMPT},
        {"role": "user", "content": text},
    ]


def parse_check(raw: str) -> Hit | None:
    """解析模型的审查结果。解析不出来就当"没违规"（宁漏不误伤）。"""
    text = (raw or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return None
    # 只认**明确的布尔真值**：`"yes"` / `1` 之类都不算命中。
    # 方向是「宁漏不误伤」——漏拦一条可以补，误禁言一个人补不回来。
    if not isinstance(data, dict) or data.get("hit") is not True:
        return None
    category = str(data.get("category") or "其他").strip() or "其他"
    return Hit(category=category, evidence=str(data.get("why") or "").strip())


def dumps_hit(hit: Hit) -> str:
    """`Hit` -> JSON 字符串（塞进 kv 做"最近拦截"列表用）。"""
    return json.dumps({"category": hit.category, "evidence": hit.evidence}, ensure_ascii=False)
