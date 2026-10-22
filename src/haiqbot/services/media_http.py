"""只读媒体服务：把 `/i/<id>` 暴露给协议端。

为什么需要它：实测（NapCat + QQ NT）**base64 / 本地路径 / file:// 发图全部被拒**
（`rich media transfer failed`），只有 **http(s) URL** 能成 —— QQ 客户端自己下载后
走"远程图片"通道，绕开了被封的上传通道。

所以：我们起一个**只读、只暴露 `/i/<id>`** 的小服务，让容器里的协议端能拉到图；
面板本身仍然只绑在 127.0.0.1，不跟着扩大暴露面。
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.responses import FileResponse, Response
from starlette.routing import Route


def build_media_app(media: Any) -> Starlette:
    async def file(request: Any) -> Response:
        media_id = str(request.path_params["media_id"])
        path = await media.get_path(media_id)
        if path is None:
            return Response(status_code=404)
        mime = await media.get_mime(media_id)
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})

    async def health(_request: Any) -> Response:
        return Response("ok", media_type="text/plain")

    return Starlette(
        routes=[Route("/i/{media_id}", file, methods=["GET"]), Route("/healthz", health, methods=["GET"])]
    )


__all__ = ["build_media_app"]
