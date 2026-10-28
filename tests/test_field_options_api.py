"""`GET /api/plugins/{pid}/options/{key}`：把插件现算的可选值端给面板。

这个端点开了「面板 → 插件 → 外部接口」的一条路，所以边界必须钉住：
- 只有 `config_schema` 里**声明过且标了 dynamic** 的字段能问；
- 只有字段自己列出的 `dynamic_inputs` 能作为覆盖值传进去（别处一律丢弃）；
- 只读：不写审计、不改配置。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from haiqbot.webui.api import plugins as plugins_api
from haiqbot.webui.api.common import Ctx

SCHEMA = [
    {"key": "chat_model", "label": "对话模型", "dynamic": True, "dynamic_inputs": ["chat_base_url"]},
    {"key": "chat_base_url", "label": "基地址"},
    {"key": "image_model", "label": "生图模型", "dynamic": True},
]


class _Registry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def config_schema(self, pid: str) -> list[dict[str, Any]]:
        return SCHEMA

    async def config_options(
        self, pid: str, key: str, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.calls.append((pid, key, dict(overrides or {})))
        return {"plugin": pid, "key": key, "options": [{"value": "m1", "label": "m1"}], "supported": True}


def _client(registry: Any) -> TestClient:
    ctx = Ctx(
        runtime=SimpleNamespace(),
        registry=registry,
        auth=None,
        config_path=Path("haiq.toml"),
    )
    return TestClient(Starlette(routes=plugins_api.routes(ctx)))


def test_declared_dynamic_field_is_served() -> None:
    reg = _Registry()
    payload = _client(reg).get("/api/plugins/assistant/options/chat_model?chat_base_url=https://x/v1").json()
    assert payload["ok"] is True
    assert payload["data"]["options"] == [{"value": "m1", "label": "m1"}]
    assert reg.calls == [("assistant", "chat_model", {"chat_base_url": "https://x/v1"})]


def test_undeclared_input_is_dropped() -> None:
    reg = _Registry()
    _client(reg).get(
        "/api/plugins/assistant/options/chat_model?chat_base_url=a&api_key_ref=HAIQ_CHAT_API_KEY"
    )
    # 只有 dynamic_inputs 里列出的才能进来；别的参数一律忽略
    assert reg.calls == [("assistant", "chat_model", {"chat_base_url": "a"})]


def test_static_field_is_rejected() -> None:
    payload = _client(_Registry()).get("/api/plugins/assistant/options/chat_base_url").json()
    assert payload["ok"] is False
    assert payload["error"]["code"] == "not_dynamic"


def test_unknown_field_is_404() -> None:
    reg = _Registry()
    resp = _client(reg).get("/api/plugins/assistant/options/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_field"
    assert reg.calls == []


def test_missing_registry_is_503() -> None:
    resp = _client(None).get("/api/plugins/assistant/options/chat_model")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "no_registry"


def test_long_override_is_truncated() -> None:
    reg = _Registry()
    _client(reg).get("/api/plugins/assistant/options/chat_model?chat_base_url=" + "a" * 900)
    assert len(reg.calls[0][2]["chat_base_url"]) == 500


@pytest.mark.parametrize("pid", ["assistant"])
def test_image_model_field_needs_no_inputs(pid: str) -> None:
    reg = _Registry()
    _client(reg).get(f"/api/plugins/{pid}/options/image_model?chat_base_url=ignored")
    assert reg.calls == [(pid, "image_model", {})]
