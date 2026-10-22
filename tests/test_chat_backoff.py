"""退避计算：指数增长 + 上限封顶。"""

from __future__ import annotations

from haiqbot_local_chat.config import ChatCfg
from haiqbot_local_chat.providers.openai_compat import MAX_BACKOFF_SECONDS, backoff_delay


def test_exponential_growth() -> None:
    assert backoff_delay(1.0, 0) == 1.0
    assert backoff_delay(1.0, 1) == 2.0


def test_capped() -> None:
    assert backoff_delay(1.0, 2) == MAX_BACKOFF_SECONDS
    assert backoff_delay(1.0, 10) == MAX_BACKOFF_SECONDS


def test_zero_base_disables_wait() -> None:
    assert backoff_delay(0.0, 5) == 0.0
    assert backoff_delay(-1.0, 0) == 0.0


def test_default_retries_policy() -> None:
    cfg = ChatCfg()
    assert cfg.retries == 4, "实测单次成功率约 62%，需要 4 次重试把失败率压下来"
    assert cfg.retry_backoff_seconds == 1.0
    total_wait = sum(backoff_delay(cfg.retry_backoff_seconds, i) for i in range(cfg.retries))
    assert total_wait <= 15.0, "最坏等待要能塞进 handler 超时里"
