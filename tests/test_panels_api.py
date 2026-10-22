"""插件自定义页 + 频道体检端点。

PanelPage 的两条底线：
1. 只有插件自己在页面里声明过的 action 才能执行（不能变成任意方法调用面）；
2. 插件写坏（抛错/返回非 dict）只应该让那一页报错，不能把面板拖垮。
"""

from __future__ import annotations

from typing import Any

from haiqbot.webui.api import panels as panel_api


class _Plugin:
    def __init__(self, pid: str, **extra: Any) -> None:
        self.id = pid
        self.name = extra.pop("name", pid)
        for key, value in extra.items():
            setattr(self, key, value)


class _Handle:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin


class _Registry:
    def __init__(self, plugins: list[Any]) -> None:
        self._handles = {p.id: _Handle(p) for p in plugins}

    def handles(self) -> dict[str, _Handle]:
        return self._handles


def _echo_like(pid: str = "demo", **extra: Any) -> _Plugin:
    def panel_page(panel: Any) -> dict[str, Any]:
        return {
            "title": "示例页",
            "sections": [
                {"type": "field_group", "title": "概况", "fields": [{"label": "a", "value": 1}]},
                {"type": "action_button", "label": "跑", "action": "ping"},
                {"type": "action_button", "label": "坏的", "action": " "},
            ],
        }

    return _Plugin(pid, panel_page=panel_page, **extra)


def test_plugin_without_panel_page_is_not_indexed():
    assert panel_api.panel_index(_Registry([_Plugin("plain")])) == []


def test_panel_index_sorted_by_order_then_pid():
    a = _echo_like("za")
    a.panel_meta = lambda: {"title": "乙", "order": 50}
    b = _echo_like("yi")
    b.panel_meta = lambda: {"title": "甲", "order": 10}
    index = panel_api.panel_index(_Registry([a, b]))
    assert [item["title"] for item in index] == ["甲", "乙"]


def test_panel_meta_falls_back_to_plugin_name():
    index = panel_api.panel_index(_Registry([_echo_like(name="回声")]))
    assert index[0]["title"] == "回声"
    assert index[0]["order"] == 100


def test_only_declared_actions_are_allowed():
    page = {
        "sections": [
            {"type": "action_button", "action": "ping"},
            {"type": "action_button", "action": "  "},
            {"type": "text", "text": "无关"},
        ]
    }
    assert panel_api.allowed_actions(page) == {"ping"}


def test_registry_without_handles_degrades_to_empty():
    class _Bare:
        pass

    assert panel_api.panel_index(_Bare()) == []


# -- HTTP：两条新端点（用真实 Starlette app，只换假 runtime/registry）------------- #


def _client(tmp_path: Any) -> tuple[Any, str]:
    from test_webui import _client as build
    from test_webui import _login

    client = build(tmp_path)
    return client, _login(client)


def test_channels_verify_endpoint(tmp_path):
    client, _csrf = _client(tmp_path)
    resp = client.get("/api/channels/verify")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert set(data) == {"problems", "summary"}
    assert data["problems"] == []
    assert set(data["summary"]) == {"error", "warn", "info"}


def test_verify_needs_session(tmp_path):
    from test_webui import _client as build

    client = build(tmp_path)
    assert client.get("/api/channels/verify").status_code == 401


def test_panels_endpoint_is_empty_without_plugin_pages(tmp_path):
    client, _csrf = _client(tmp_path)
    resp = client.get("/api/panels")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


def test_unknown_panel_is_404(tmp_path):
    client, _csrf = _client(tmp_path)
    assert client.get("/api/panels/nope").status_code == 404


def test_panel_action_needs_csrf(tmp_path):
    client, _csrf = _client(tmp_path)
    resp = client.post("/api/panels/nope/action", json={"action": "ping"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf"


def test_panel_action_unknown_plugin_is_404(tmp_path):
    client, csrf = _client(tmp_path)
    resp = client.post("/api/panels/nope/action", json={"action": "ping"}, headers={"X-Haiq-CSRF": csrf})
    assert resp.status_code == 404
