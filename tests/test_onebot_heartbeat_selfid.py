"""OneBot 适配器的自身身份与心跳。

两件事：
1. `[onebot] self_id` 是 bot_id 的兜底来源 —— 没有它，http/sse 模式下可能永远不知道
   自己是谁，`at_bot` / 面板显示 / 过滤自己的消息都跟着失准。
2. heartbeat 不只是噪音：TCP 没断但 WS 假死时，`connected=True` 会骗过面板，
   心跳停了才是半开连接的证据。
"""

from __future__ import annotations

import time

from haiqbot.adapters.onebot11 import OneBotAdapter
from haiqbot.config import HaiqConfig, MediaCfg, OneBotCfg, PolicyCfg, QuotaCfg
from haiqbot.kernel.bus import Bus
from haiqbot.kernel.governance import AuditLog, Idempotency, QuotaService, RoleResolver
from haiqbot.kernel.runtime import Runtime


def _adapter(**cfg_kw) -> OneBotAdapter:
    return OneBotAdapter(OneBotCfg(**cfg_kw), token_provider=lambda: None)


def _group_message(**over) -> dict:
    payload = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 674425800,
        "user_id": 10001,
        "message_id": 42,
        "message": "hi",
        "sender": {"nickname": "小明", "role": "member"},
    }
    payload.update(over)
    return payload


# --------------------------------------------------------------------------- #
# self_id
# --------------------------------------------------------------------------- #
def test_self_id_from_config_used_when_payload_lacks_it() -> None:
    """payload 没有 self_id 时，配置值要能撑起 at_bot 判定。"""
    adapter = _adapter(self_id="1291944421")
    event = adapter._parse_message(
        _group_message(
            message=[
                {"type": "at", "data": {"qq": "1291944421"}},
                {"type": "text", "data": {"text": " /echo hi"}},
            ],
        )
    )
    assert event is not None
    assert event.bot_id == "1291944421"
    assert event.at_bot is True, "配置里有 self_id 才能算出「被 @ 了」"


def test_self_id_from_payload_wins() -> None:
    """payload 里的 self_id 更新，且会覆盖配置。"""
    adapter = _adapter(self_id="111")
    event = adapter._parse_message(_group_message(self_id=222))
    assert event is not None
    assert event.bot_id == "222"


def test_self_message_is_dropped() -> None:
    """协议端把机器人自己发的消息当普通 message 推上来 → 必须丢，否则自问自答。"""
    adapter = _adapter(self_id="1291944421")
    assert adapter._parse_message(_group_message(user_id=1291944421)) is None


def test_other_user_message_survives() -> None:
    adapter = _adapter(self_id="1291944421")
    event = adapter._parse_message(_group_message(user_id=10001))
    assert event is not None
    assert event.actor.id == "10001"


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_records_time_interval_and_identity() -> None:
    adapter = _adapter()
    assert adapter._last_heartbeat is None

    event = adapter._parse_meta(
        {"post_type": "meta_event", "meta_event_type": "heartbeat", "self_id": 1291944421, "interval": 10000}
    )
    assert event is None, "心跳不进管线（每 30s 一条，纯噪音）"
    assert adapter._last_heartbeat is not None
    assert adapter._heartbeat_interval == 10.0, "interval 是毫秒"
    assert adapter.bot_id == "1291944421", "心跳也带 self_id，顺手记下身份"


def test_heartbeat_without_interval_keeps_default() -> None:
    adapter = _adapter()
    adapter._parse_meta({"meta_event_type": "heartbeat", "self_id": 1})
    assert adapter._heartbeat_interval == 30.0


def test_health_heartbeat_stale_is_none_before_any_heartbeat() -> None:
    """没收到过心跳就别声称「假死」—— 未知态不谎报。"""
    assert _adapter().health()["heartbeat_stale"] is None


def test_health_heartbeat_fresh_and_stale() -> None:
    adapter = _adapter()
    adapter._connected.set()

    adapter._last_heartbeat = time.time()
    health = adapter.health()
    assert health["heartbeat_stale"] is False
    assert health["connected"] is True
    assert health["heartbeat_interval"] == 30.0

    adapter._last_heartbeat = time.time() - 200  # 远超 2.5×30 与 90 秒下限
    assert adapter.health()["heartbeat_stale"] is True


def test_heartbeat_threshold_follows_interval() -> None:
    """间隔 120s 的协议端，2 分钟没心跳不算假死。"""
    adapter = _adapter()
    adapter._parse_meta({"meta_event_type": "heartbeat", "self_id": 1, "interval": 120000})
    adapter._last_heartbeat = time.time() - 200
    assert adapter.health()["heartbeat_stale"] is False


async def test_runtime_marks_degraded_on_stale_heartbeat() -> None:
    """半开连接：connected 还是 True，但整体必须判降级。"""
    cfg = HaiqConfig(policy=PolicyCfg(), quota=QuotaCfg(), media=MediaCfg())
    resolver = RoleResolver.from_policy(cfg.policy)
    rt = Runtime(
        config=cfg,
        store=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        bus=Bus(),
        resolver=resolver,
        quota=QuotaService(None, cfg.quota, resolver),  # type: ignore[arg-type]
        idempotency=Idempotency(None),  # type: ignore[arg-type]
        audit=AuditLog(None),  # type: ignore[arg-type]
    )
    adapter = _adapter(self_id="1291944421")
    adapter._connected.set()  # TCP 还连着
    adapter._last_heartbeat = time.time() - 200  # 但心跳早停了
    rt.adapters = {"onebot": adapter}

    health = rt.health()
    assert health["platforms"]["onebot"]["health"]["connected"] is True
    assert health["ok"] is False, "半开连接必须让整体判为不健康"
