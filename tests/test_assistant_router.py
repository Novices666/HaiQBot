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
        # 模型契约允许的方向词（正则只负责翻译这几个枚举）
        ("landscape", 16 / 9, "landscape"),
        ("portrait", 9 / 16, "portrait"),
        ("square", 1.0, "square"),
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
        (16 / 9, "1920x1080"),  # 精确命中 16:9 档
        (3 / 2, "1536x1024"),  # 精确命中
        (1.0, "1024x1024"),
        (9 / 16, "1080x1920"),  # 竖图：对数距离才不会判成方图
        (1920 / 1080, "1920x1080"),
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
    assert router.describe_size("1920x1080") == "1920×1080"


# --------------------------------------------------------------------------- #
# 尺寸：最终决策（原句 > 模型方向词 > 默认档）
# --------------------------------------------------------------------------- #
def test_decide_size_direction_word_when_hint_missing() -> None:
    """模型没给 hint 时，原句里的方向词仍能兜底（"横版"是枚举，不是语义猜测）。"""
    key, parsed = router.decide_size("画一张横版壁纸", None)
    assert key == "1920x1080"
    assert parsed is not None and parsed.label == "横版"


def test_colloquial_object_words_are_left_to_the_model() -> None:
    """口语里的对象（电脑壁纸/手机壁纸）**不由正则解释** —— 那是语义理解，归模型。

    这条钉死分工：如果哪天有人又把"电脑壁纸"塞回词表，这里会红，
    提醒他"两套口径"的问题（实测模型对这类说法 6/6 都给了 landscape）。
    """
    for text in ("画一张电脑壁纸", "来张手机壁纸", "给我做个 pc 壁纸"):
        assert router.parse_size(text) is None


def test_decide_size_explicit_beats_word_table() -> None:
    """原句里同时有词和显式比例时，显式比例优先（parse_size 内部顺序）。"""
    key, parsed = router.decide_size("画一张竖版壁纸 16:9", None)
    assert key == "1920x1080"
    assert parsed is not None and parsed.label == "16:9"


def test_decide_size_falls_back_to_model_hint() -> None:
    """原句只有"来张壁纸吧"，没有任何尺寸词 → 用模型给的方向词补位。"""
    key, parsed = router.decide_size("来张壁纸吧", "landscape")
    assert key == "1920x1080"
    assert parsed is not None and parsed.label == "landscape"


def test_decide_size_model_hint_wins() -> None:
    """尺寸判断完全交给模型：模型给的方向词优先于原句的正则词。

    这是用户明确要求的行为。代价是模型若判断错，代码不会替它纠正 ——
    唯一保留的保险是 `absorb_size`（结果一定落在合法档位，不会 400）。
    """
    key, parsed = router.decide_size("画一张竖版壁纸", "landscape")
    assert key == "1920x1080"
    assert parsed is not None and parsed.label == "landscape"


def test_decide_size_unparsable_hint_falls_back_to_original() -> None:
    """模型给了看不懂的东西（"大一点"）时，不能让尺寸凭空丢失。"""
    key, parsed = router.decide_size("画一张横版壁纸", "大一点")
    assert key == "1920x1080"
    assert parsed is not None and parsed.label == "横版"


def test_decide_size_hint_still_snapped_to_legal_tier() -> None:
    """比例/方向词必须吸附到档位（接口不认 "21:9" 这种字符串）。"""
    key, _ = router.decide_size("随便画", "21:9")
    assert key in router.SIZES


def test_explicit_resolution_is_passed_through() -> None:
    """用户明确写的分辨率原样透传，不被静默改成别的档位。

    实测：接口对非预设值同样返回 200 并精确返回所请求尺寸（1280x720 也测过）。
    """
    key, parsed = router.decide_size("画一张 1280x720 的猫", None)
    assert key == "1280x720"
    assert parsed is not None and parsed.kind == "res"


def test_dim_annotation_is_passed_through() -> None:
    key, _ = router.decide_size("画一只猫 宽1280 高720", None)
    assert key == "1280x720"


def test_explicit_resolution_over_cap_falls_back_to_tier() -> None:
    """超过体积闸门的显式分辨率要压回档位 —— 4K 出 12.6MB，发群风险大。"""
    key, _ = router.decide_size("画一张 3840x2160 的壁纸", None)
    assert key == "1920x1080"


def test_describe_size_handles_passthrough_values() -> None:
    """透传值不在 SIZES 里，describe_size 仍要能显示成人话。"""
    assert router.describe_size("1280x720") == "1280×720"
    assert router.describe_size("1920x1080") == "1920×1080"


def test_decide_size_uses_default_when_nothing_mentioned() -> None:
    key, parsed = router.decide_size("画一只猫", None)
    assert key == router.DEFAULT_SIZE
    assert parsed is None


def test_decide_size_respects_configured_default() -> None:
    key, _ = router.decide_size("画一只猫", None, default="1536x1024")
    assert key == "1536x1024"


def test_decide_size_rejects_illegal_configured_default() -> None:
    """配置里写了个接口不认的值 → 回落默认档，绝不透传（透传会 400）。"""
    key, _ = router.decide_size("画一只猫", None, default="9999x1")
    assert key == router.DEFAULT_SIZE


# --------------------------------------------------------------------------- #
# 决策锁：few-shot 是被实测删掉的，不是忘了写
# --------------------------------------------------------------------------- #


def test_few_shot_stays_removed() -> None:
    """FEW_SHOT 必须保持为空 —— 加回来要拿新证据，不是顺手。

    2026-10-25 对照实测（deepseek-v4.1-flash）：
      - 10 句边界用例 × 2 轮：带满 6 条示范判定 100%，一条不留也是 100%；
      - 6 句 image 用例 × 3 轮：全删后 reply 变长（均值 19.6 → 29.4 字），
        但判定一次没失手 —— 而"reply 长几句"不值得每次多送 800 token。
    所以整段移除。若将来换了模型、判定开始掉，先补回示范再说话，
    同时把 SYSTEM_PROMPT 里对应的规则一起改，别让规则和示范打架。
    """
    assert router.FEW_SHOT == ()


def test_build_messages_has_no_few_shot_noise() -> None:
    """消息体只该是 system + 历史 + 本次提问，不该混进示范对话。"""
    msgs = router.build_messages("画一只猫")
    assert msgs[0]["role"] == "system"
    assert msgs[-1] == {"role": "user", "content": "画一只猫"}
    assert len(msgs) == 2, msgs

    with_history = router.build_messages(
        "再说一次", [{"role": "user", "content": "画一只猫"}, {"role": "assistant", "content": "好"}]
    )
    assert len(with_history) == 4


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
    # 正例/反例的职责已从 few-shot 移到 SYSTEM_PROMPT 的第 1、2 条（见上面的决策锁）：
    # 这里只断言"该讲的都在提示词里"，不再假设存在示范对话。
    joined = " ".join(m["content"] for m in msgs)
    assert "action=image" in joined
    assert "action=text" in joined
    # 历史要在最后一条 user 之前
    assert {"role": "user", "content": "上一句"} in msgs
