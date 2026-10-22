"""媒体中转库。

**为什么必须存在这个东西**：

生图 provider 往往返回带签名参数的临时 URL（`?X-Tos-Signature=…`）。
如果把这个 URL 直接丢给平台去拉，签名就进了平台日志、也进了群友能抓到的包。
所以这里统一：下载 → 落盘 → 重新发牌一个**无参数、不可枚举**的短 ID。

对外只有 `/i/{id}`，没有查询串，爆破不了。
"""
from __future__ import annotations
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from haiqbot.config import MediaCfg, WebuiCfg
from haiqbot.kernel.log import get_logger
log = get_logger(__name__)

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

@dataclass(frozen=True, slots=True)
class MediaRef:
    id: str
    path: Path
    mime: str
    url: str

class MediaStore:
    def __init__(
        self,
        store: Any,
        cfg: MediaCfg,
        *,
        webui: WebuiCfg | None = None,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.dir = Path(cfg.dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._webui = webui or WebuiCfg()
    # -- 地址 ------------------------------------------------------------- #
    @property
    def base_url(self) -> str:
        if self.cfg.public_base_url:
            return self.cfg.public_base_url.rstrip("/")
        return f"http://{self._webui.host}:{self._webui.port}"
    def url_for(self, media_id: str) -> str:
        return f"{self.base_url}/i/{media_id}"
    # -- 写入 ------------------------------------------------------------- #
    async def put(
        self,
        data: bytes,
        mime: str = "image/png",
        *,
        channel: str | None = None,
        plugin_id: str | None = None,
    ) -> MediaRef:
        media_id = secrets.token_urlsafe(16)
        ext = _EXT_BY_MIME.get(mime.lower(), ".bin")
        path = self.dir / f"{media_id}{ext}"
        path.write_bytes(data)
        await self.store.media_add(media_id, str(path), mime, float(self.cfg.ttl_seconds), channel, plugin_id)
        log.info("media_stored", media_id=media_id, bytes=len(data), mime=mime)
        return MediaRef(id=media_id, path=path, mime=mime, url=self.url_for(media_id))
    # -- 读取 ------------------------------------------------------------- #
    async def get_path(self, media_id: str) -> Path | None:
        if not _looks_valid(media_id):
            return None
        row = await self.store.media_get(media_id)
        if not row:
            return None
        path = Path(row["path"])
        if not path.is_file():
            return None
        return path
    async def get_mime(self, media_id: str) -> str:
        row = await self.store.media_get(media_id)
        return str(row["mime"]) if row else "application/octet-stream"
    # -- 清理 ------------------------------------------------------------- #
    async def gc(self) -> int:
        expired = await self.store.media_expired()
        removed = 0
        for row in expired:
            try:
                Path(row["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            await self.store.media_delete(row["id"])
            removed += 1
        if removed:
            log.info("media_gc", removed=removed, at=int(time.time()))
        return removed

def _looks_valid(media_id: str) -> bool:
    if not media_id or len(media_id) > 64:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in media_id)
