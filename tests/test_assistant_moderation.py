"""内容审查：词表/正则（确定性部分）+ 模型结果解析。

模型判定本身不进 CI（要真调），这里钉的是：
命中就是命中、配置解析正确、模型返回坏掉时**宁漏不误伤**。
"""

from __future__ import annotations

import pytest
from haiqbot_local_assistant import moderation


def test_lexicon_hit_finds_word() -> None:
    hit = moderation.lexicon_hit("加微信 abc123 拉你进群", ("加微信",))
    assert hit is not None
    assert hit.evidence == "加微信"


def test_lexicon_hit_is_case_insensitive() -> None:
    assert moderation.lexicon_hit("请加V：xxx", (), moderation.DEFAULT_PATTERNS) is not None


def test_lexicon_hit_matches_phone_number() -> None:
    hit = moderation.lexicon_hit("联系我 13800138000", (), moderation.DEFAULT_PATTERNS)
    assert hit is not None


def test_lexicon_hit_ignores_longer_numbers() -> None:
    """前后紧邻数字的长串不算手机号 —— 否则订单号会被误伤。"""
    assert moderation.lexicon_hit("订单 91380013800012", (), moderation.DEFAULT_PATTERNS) is None


def test_lexicon_hit_clean_text() -> None:
    assert (
        moderation.lexicon_hit("今天天气真好", moderation.DEFAULT_WORDS, moderation.DEFAULT_PATTERNS) is None
    )


def test_lexicon_hit_empty_text() -> None:
    assert moderation.lexicon_hit("", ("加微信",)) is None


def test_broken_pattern_does_not_crash() -> None:
    """配置里写坏正则不该让插件的每条消息都炸。"""
    assert moderation.lexicon_hit("随便说说", (), ("([",)) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("加微信,私聊我", ("加微信", "私聊我")),
        ("加微信，私聊我", ("加微信", "私聊我")),  # 全角逗号
        ("加微信\n私聊我", ("加微信", "私聊我")),
        (["a", " b "], ("a", "b")),
        ("", ()),
        (None, ()),
    ],
)
def test_words_from_config(raw: object, expected: tuple[str, ...]) -> None:
    assert moderation.words_from_config(raw) == expected


def test_parse_check_true() -> None:
    hit = moderation.parse_check('{"hit":true,"category":"广告引流","why":"拉人"}')
    assert hit is not None and hit.category == "广告引流"


@pytest.mark.parametrize(
    "raw",
    [
        '{"hit":false}',
        "{}",
        "不是 JSON",
        "",
        '{"hit":"yes"}',  # 非布尔真值不算命中
    ],
)
def test_parse_check_never_blocks_on_garbage(raw: str) -> None:
    """解析不出来就当没违规：误拦正常群友比漏拦一条更糟。"""
    assert moderation.parse_check(raw) is None


def test_parse_check_tolerates_fence() -> None:
    hit = moderation.parse_check('```json\n{"hit":true,"category":"辱骂"}\n```')
    assert hit is not None and hit.category == "辱骂"


def test_check_messages_has_system_and_user() -> None:
    msgs = moderation.build_check_messages("测试文本")
    assert msgs[0]["role"] == "system"
    assert "只输出一个 JSON 对象" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "测试文本"}
