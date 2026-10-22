"""生图 Broker + 媒体中转：干净 URL、落盘、GC、错误不泄露密钥。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from haiqbot_local_painter.broker import ImageBroker
from haiqbot_local_painter.config import ImageCfg
from haiqbot_local_painter.providers.mock import MockImageProvider
from haiqbot_local_painter.providers.openai_compat import OpenAICompatProvider

from haiqbot.config import MediaCfg
from haiqbot.kernel.vault import PlainFileBackend, Vault
from haiqbot.services.media_store import MediaStore
from haiqbot.services.providers.base import ProviderError

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


async def test_broker_returns_clean_url_and_persists(store, cfg) -> None:

    media = MediaStore(store, cfg.media, webui=cfg.webui)

    broker = ImageBroker(MockImageProvider(), media)

    ref = await broker.generate("一只橘猫")

    assert ref.media_id

    assert ref.url.endswith(f"/i/{ref.media_id}")

    assert "?" not in ref.url, "对外 URL 不允许带任何查询串（防签名泄露）"

    assert "token" not in ref.url.lower()

    path = await media.get_path(ref.media_id)

    assert path is not None and path.is_file()

    assert await store.media_count() == 1


async def test_media_id_is_unguessable(store, cfg) -> None:

    media = MediaStore(store, cfg.media, webui=cfg.webui)

    ids = {(await media.put(b"x", "image/png")).id for _ in range(20)}

    assert len(ids) == 20

    assert all(len(i) >= 16 for i in ids)


async def test_media_rejects_traversal(store, cfg) -> None:

    media = MediaStore(store, cfg.media, webui=cfg.webui)

    assert await media.get_path("../../etc/passwd") is None

    assert await media.get_path("a" * 100) is None


async def test_media_gc_removes_expired(store, cfg) -> None:

    short = MediaCfg(dir=cfg.media.dir, ttl_seconds=0)

    media = MediaStore(store, short, webui=cfg.webui)

    ref = await media.put(PNG_1PX, "image/png")

    path = await media.get_path(ref.id)

    assert path is not None

    removed = await media.gc()

    assert removed == 1

    assert await store.media_count() == 0

    assert not path.exists()


# --------------------------------------------------------------------------- #


def _auth(vault, ref: str = "WEFT_IMAGE_API_KEY"):
    """把 vault 适配成 provider 需要的鉴权头生成器。"""

    return lambda: {"Authorization": f"Bearer {vault.require(ref)}"}


def _client(handler) -> httpx.AsyncClient:

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_provider_base64_path(tmp_path) -> None:

    vault = Vault([PlainFileBackend(tmp_path / "v.json")])

    vault.set("WEFT_IMAGE_API_KEY", "sk-test-key-abcdefghijkl")

    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:

        seen.append(
            {
                "auth": request.headers.get("Authorization"),
                "body": json.loads(request.content),
            }
        )

        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]})

    cfg = ImageCfg(base_url="https://api.example.com/v1", model="m", api_key_ref="WEFT_IMAGE_API_KEY")

    provider = OpenAICompatProvider(_auth(vault), cfg, client=_client(handler))

    data, mime = await provider.generate("cat", "512x512")

    assert data == PNG_1PX

    assert mime == "image/png"

    assert seen[0]["auth"] == "Bearer sk-test-key-abcdefghijkl"

    assert seen[0]["body"]["size"] == "512x512"


async def test_provider_url_path_is_downloaded(tmp_path) -> None:

    vault = Vault([PlainFileBackend(tmp_path / "v.json")])

    vault.set("WEFT_IMAGE_API_KEY", "sk-test-key-abcdefghijkl")

    def handler(request: httpx.Request) -> httpx.Response:

        if request.url.host == "cdn.example.com":
            return httpx.Response(200, content=PNG_1PX, headers={"content-type": "image/png"})

        return httpx.Response(200, json={"data": [{"url": "https://cdn.example.com/a.png?sig=SECRET"}]})

    cfg = ImageCfg(base_url="https://api.example.com/v1", api_key_ref="WEFT_IMAGE_API_KEY")

    provider = OpenAICompatProvider(_auth(vault), cfg, client=_client(handler))

    data, mime = await provider.generate("cat")

    assert data == PNG_1PX

    assert mime == "image/png"


async def test_provider_error_does_not_leak_key(tmp_path) -> None:

    vault = Vault([PlainFileBackend(tmp_path / "v.json")])

    secret = "sk-leaky-key-0987654321"

    vault.set("WEFT_IMAGE_API_KEY", secret)

    def handler(request: httpx.Request) -> httpx.Response:

        return httpx.Response(401, json={"error": f"invalid key {secret}"})

    cfg = ImageCfg(base_url="https://api.example.com/v1", api_key_ref="WEFT_IMAGE_API_KEY")

    provider = OpenAICompatProvider(_auth(vault), cfg, client=_client(handler))

    with pytest.raises(ProviderError) as exc:
        await provider.generate("cat")

    assert secret not in str(exc.value)

    assert "401" in str(exc.value)


async def test_provider_missing_key_raises(tmp_path) -> None:

    vault = Vault([PlainFileBackend(tmp_path / "v.json")])

    cfg = ImageCfg(api_key_ref="NOT_SET")

    provider = OpenAICompatProvider(
        _auth(vault, "NOT_SET"), cfg, client=_client(lambda r: httpx.Response(200))
    )

    with pytest.raises(Exception) as exc:
        await provider.generate("cat")

    assert "NOT_SET" in str(exc.value)
