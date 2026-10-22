"""只读媒体服务：只暴露 /i/<id>，未知 id 404。"""

from __future__ import annotations

import pathlib

from starlette.testclient import TestClient

from haiqbot.services.media_http import build_media_app


class FakeMedia:
    def __init__(self, base: pathlib.Path) -> None:
        self.base = base
        self.png = base / "a.png"
        self.png.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    async def get_path(self, media_id: str):
        return self.png if media_id == "abc" else None

    async def get_mime(self, media_id: str) -> str:
        return "image/png"


def test_serves_known_media(tmp_path: pathlib.Path) -> None:
    client = TestClient(build_media_app(FakeMedia(tmp_path)))
    resp = client.get("/i/abc")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")


def test_unknown_media_is_404(tmp_path: pathlib.Path) -> None:
    client = TestClient(build_media_app(FakeMedia(tmp_path)))
    assert client.get("/i/nope").status_code == 404


def test_healthz(tmp_path: pathlib.Path) -> None:
    client = TestClient(build_media_app(FakeMedia(tmp_path)))
    assert client.get("/healthz").status_code == 200
