"""配置上限真正生效 + `haiqbot init` 模板从插件 schema 生成。"""

from __future__ import annotations

import tomllib

import pytest
from haiqbot_local_assistant.chat.broker import LLMBroker
from haiqbot_local_assistant.chat.providers.mock import MockChatProvider
from haiqbot_local_assistant.image.broker import ImageBroker
from haiqbot_local_assistant.image.providers.mock import MockImageProvider

from haiqbot.config import dump_default_config
from haiqbot.services.providers.base import ProviderError


# --------------------------------------------------------------------------- #
# 上限校验（以前是"死配置"：能改但没人读）
# --------------------------------------------------------------------------- #
async def test_painter_rejects_too_long_prompt() -> None:
    broker = ImageBroker(MockImageProvider(), object())
    broker.max_prompt = 5
    with pytest.raises(ProviderError) as exc:
        await broker.generate("x" * 6)
    assert "提示词太长" in str(exc.value)


async def test_painter_allows_prompt_within_limit() -> None:
    broker = ImageBroker(MockImageProvider(), object())
    broker.max_prompt = 10
    # 没有 media 会失败，但错误不该是"提示词太长"——说明校验放行了
    with pytest.raises(Exception) as exc:
        await broker.generate("x" * 10)
    assert "提示词太长" not in str(exc.value)


async def test_chat_rejects_too_long_input() -> None:
    broker = LLMBroker(MockChatProvider(), model="m", system_prompt="S")
    broker.max_input_chars = 5
    with pytest.raises(ProviderError) as exc:
        await broker.chat("x" * 6)
    assert "输入太长" in str(exc.value)


async def test_chat_allows_input_within_limit() -> None:
    broker = LLMBroker(MockChatProvider(), model="m", system_prompt="S")
    broker.max_input_chars = 50
    assert await broker.chat("hi") == "（模拟回复）hi"


async def test_chat_zero_limit_means_unlimited() -> None:
    broker = LLMBroker(MockChatProvider(), model="m", system_prompt="S")
    broker.max_input_chars = 0
    assert await broker.chat("x" * 5000)


# --------------------------------------------------------------------------- #
# init 模板：默认值单一来源（插件 config_schema）
# --------------------------------------------------------------------------- #
def test_default_config_is_valid_toml() -> None:
    text = dump_default_config()
    data = tomllib.loads(text)
    # 插件参数已外置到 data/plugins/<id>.toml，主模板只剩注册信息
    assert set(data["plugins"]) == {"enabled"}
    assert "data/plugins/" in text


def test_default_config_contains_no_secret_values() -> None:
    text = dump_default_config()
    assert "sk-" not in text
    # 只允许出现 *_ref 键名
    data = tomllib.loads(text)
    for section in data["plugins"].values():
        for key in section:
            assert not key.endswith("_key"), f"疑似真值字段：{key}"


def test_plugin_sections_generated_from_schema() -> None:
    from haiqbot.config import _plugin_schemas

    schemas = _plugin_schemas()
    assert "assistant" in schemas
    assistant_keys = {field["key"] for field in schemas["assistant"]}
    assert "image_max_prompt_chars" in assistant_keys
    assert "chat_max_input_chars" in assistant_keys
