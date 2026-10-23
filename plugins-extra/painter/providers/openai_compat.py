"""OpenAI 兼容的 `/images/generations` provider。

兼容但不限于：OpenAI 官方、各类中转网关、自建 one-api 等。
密钥只从 vault 取，取完立刻进入脱敏名单；本模块任何异常信息都不含密钥。
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from typing import Any

from haiqbot.plugin_api import ProviderError, get_logger

from ..config import ImageCfg

log = get_logger(__name__)


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        auth: Callable[[], dict[str, str]],
        cfg: ImageCfg,
        *,
        client: Any = None,
        timeout: float | None = None,
    ) -> None:
        # auth 是内核发放的鉴权头生成器：provider 完全不接触密钥本身
        self._auth = auth
        self.cfg = cfg
        self._client = client
        self.timeout = timeout if timeout is not None else cfg.timeout_seconds

    # -- HTTP ------------------------------------------------------------- #
    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        import httpx

        self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            await self._client.aclose()
            self._client = None

    # -- 主流程 ----------------------------------------------------------- #
    async def generate(self, prompt: str, size: str | None = None) -> tuple[bytes, str]:
        url = self.cfg.base_url.rstrip("/") + "/images/generations"
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "prompt": prompt,
            "n": 1,
            "size": size or self.cfg.size,
        }
        headers = self._auth()
        client = self._http()
        try:
            resp = await client.post(url, json={**payload, "response_format": "b64_json"}, headers=headers)
            if resp.status_code in (400, 422):  # 有些网关不认 response_format
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 网络层异常，可能带 URL
            raise ProviderError(f"生图请求失败：{type(exc).__name__}") from exc
        if resp.status_code >= 400:
            # 只暴露状态码与业务错误码，绝不回显请求体或 header
            raise ProviderError(f"生图接口返回 {resp.status_code}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("生图接口返回了非 JSON 响应") from exc
        return await self._extract(body)

    async def _extract(self, body: dict[str, Any]) -> tuple[bytes, str]:
        items = body.get("data") or []
        if not items:
            raise ProviderError("生图接口未返回图片数据")
        item = items[0]
        if b64 := item.get("b64_json"):
            try:
                return base64.b64decode(b64), "image/png"
            except (binascii.Error, ValueError) as exc:
                raise ProviderError("图片 base64 解码失败") from exc
        if link := item.get("url"):
            return await self._download(link)
        raise ProviderError("生图接口返回体既无 b64_json 也无 url")

    async def _download(self, link: str) -> tuple[bytes, str]:
        """把带签名的临时 URL 拉成字节 —— 这个 URL 不会外传。"""
        client = self._http()
        try:
            resp = await client.get(link)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"下载图片失败：{type(exc).__name__}") from exc
        if resp.status_code >= 400:
            raise ProviderError(f"下载图片返回 {resp.status_code}")
        mime = resp.headers.get("content-type", "image/png").split(";")[0].strip() or "image/png"
        return resp.content, mime
