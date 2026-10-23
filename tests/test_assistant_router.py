"""意图路由与尺寸解析：这些是纯函数，直接决定「这次要不要烧生图额度」。

判断准确率没法在 CI 里测（要真调模型），但**所有确定性部分**都必须钉死：
尺寸抽取、吸附、剥离、JSON 校验、降级方向。
"""

from __future__ import annotations

import pytest
from haiqbot_local_assistant import router


# --------------------------------------------------------------------------- #
# 尺寸：抽取
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "ratio", "label"),
    [
        ("画一只猫 16:9", 16 / 9, "16:9"),
        ("画一只猫 16：9", 16 / 9, "16:9"),  # 全角冒号
        ("画一只猫 16比9", 16 / 9, "16:9"),
        ("画一只猫 1920x1080", 1920 / 1080, "1920x1080"),
        ("画一只猫 1920*1080", 1920 / 1080, "1920x1080"),
        ("画一只猫 1920×1080", 1920 / 1080, "1920x1080"),
        ("画一只猫 1920 X 1080", 1920 / 1080, "1920x1080"),
        ("画一只猫 宽1920 高1080", 1920 / 1080, "宽1920 高1080"),
        ("画一只猫 竖版", 9 / 16, "竖版"),
        ("画一只猫 方图", 1.0, "方图"),
    ],
)
def test_parse_size_variants(text: str, ratio: float, label: str) -> None:
    hint = router.parse_size(text)
    assert hint is not None
    assert hint.ratio == pytest.approx(ratio)
    assert hint.label == label


@pytest.mark.parametrize(
    "text",
    [
        "画一只猫",
        "",
        "画个长发少女",  # "长"后面不是数字，不能被当成尺寸
        "画一只猫 8K",  # 没有具体数字维度，不猜
    ],
)
def test_parse_size_returns_none_without_size(text: str) -> None:
    assert router.parse_size(text) is None


def test_parse_size_strips_every_size_token() -> None:
    """同一句里写了两种尺寸时，两种都要从描述里剥掉。"""
    text = "画一只猫 16:9 1920x1080"
    hint = router.parse_size(text)
    assert hint is not None
    cleaned = router.strip_spans(text, hint.spans)
    assert "16:9" not in cleaned
    assert "1920x1080" not in cleaned
    assert cleaned.startswith("画一只猫")


def test_strip_spans_keeps_non_size_words() -> None:
    text = "画一只长发少女，竖版"
    hint = router.parse_size(text)
    assert hint is not None
    cleaned = router.strip_spans(text, hint.spans)
    assert "长发" in cleaned, "不能把「长发」当成尺寸切掉"
    assert "竖版" not in cleaned


# --------------------------------------------------------------------------- #
# 尺寸：吸附
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (16 / 9, "1536x1024"),  # 1.78 -> 最近的 1.5
        (3 / 2, "1536x1024"),  # 精确命中
        (1.0, "1024x1024"),
        (9 / 16, "1024x1536"),  # 竖图：对数距离才不会判成方图
        (1920 / 1080, "1536x1024"),
        (0.0, "1024x1024"),  # 非法输入兜底
    ],
)
def test_absorb_size(ratio: float, expected: str) -> None:
    assert router.absorb_size(ratio) == expected


def test_absorb_only_returns_legal_sizes() -> None:
    """无论用户要什么比例，结果必须落在合法档位里 —— 透传非法尺寸会 400。"""
    for ratio in (0.1, 0.5, 1.0, 1.7, 2.5, 10.0):
        assert router.absorb_size(ratio) in router.SIZES


def test_describe_size_is_human_readable() -> None:
    assert router.describe_size("1536x1024") == "1536×1024"


# --------------------------------------------------------------------------- #
# 意图：模型返回的校验
# --------------------------------------------------------------------------- #
def test_parse_decision_accepts_plain_json() -> None:
    raw = (
        '{"action":"image","reply":"好的","prompt":"一只猫","size_hint":"16:9",'
        '"confidence":0.9,"why":"明确请求","violation":{"hit":false}}'
    )
    d = router.parse_decision(raw)
    assert d is not None
    assert d.wants_image is True
    assert d.prompt == "一只猫"
    assert d.size_hint == "16:9"
    assert d.violation is None


def test_parse_decision_tolerates_markdown_fence() -> None:
    raw = (
        "```json\n"
        '{"action":"text","reply":"这是回答","prompt":null,"confidence":0.7,"violation":{"hit":false}}\n'
        "```"
    )
    d = router.parse_decision(raw)
    assert d is not None and d.action == "text" and d.reply == "这是回答"


def test_parse_decision_tolerates_surrounding_prose() -> None:
    raw = '好的，我的判断是：{"action":"text","reply":"嗨","confidence":0.6} 以上。'
    d = router.parse_decision(raw)
    assert d is not None and d.reply == "嗨"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "完全没有 JSON",
        '{"action":"draw","reply":"x","prompt":"y"}',  # action 非法
        '{"action":"image","reply":"x","prompt":null}',  # 说要画却没描述
        '{"action":"image","reply":"x"}',
        '{"action":"text"}',  # reply 缺了 → 由调用方兜底，这里仍算合法
    ],
)
def test_parse_decision_rejects_or_degrades(raw: str) -> None:
    """解析不出来返回 None = 调用方降级为纯文本。**绝不在看不懂的返回上出图。**"""
    d = router.parse_decision(raw)
    if raw == '{"action":"text"}':
        assert d is not None and d.action == "text"
        return
    assert d is None


def test_parse_decision_clamps_confidence() -> None:
    d = router.parse_decision('{"action":"text","reply":"x","confidence":7}')
    assert d is not None and d.confidence == 1.0
    d = router.parse_decision('{"action":"text","reply":"x","confidence":-3}')
    assert d is not None and d.confidence == 0.0
    d = router.parse_decision('{"action":"text","reply":"x","confidence":"n/a"}')
    assert d is not None and d.confidence == 0.0


def test_parse_decision_reads_violation() -> None:
    raw = (
        '{"action":"text","reply":"x","confidence":0.9,'
        '"violation":{"hit":true,"category":"广告引流","why":"拉人进群"}}'
    )
    d = router.parse_decision(raw)
    assert d is not None and d.violation is not None
    assert d.violation.category == "广告引流"


def test_parse_decision_image_without_prompt_is_unexecutable() -> None:
    """模型说要画但没给描述 → 不执行（否则会拿空串去请求生图）。"""
    assert router.parse_decision('{"action":"image","reply":"x","prompt":"  "}') is None


# --------------------------------------------------------------------------- #
# 消息组装
# --------------------------------------------------------------------------- #
def test_build_messages_shape() -> None:
    msgs = router.build_messages("画一只猫", [{"role": "user", "content": "上一句"}])
    assert msgs[0]["role"] == "system"
    assert "只输出一个 JSON 对象" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "画一只猫"}
    # few-shot 里必须同时有正例和反例，否则模型会倾向于"顺着用户画"
    joined = " ".join(m["content"] for m in msgs)
    assert "帮我画一张小猫图吧" in joined
    assert "帮我生成一个 UUID" in joined
    assert "draw a conclusion" in joined
    # 历史要在最后一条 user 之前
    assert {"role": "user", "content": "上一句"} in msgs
