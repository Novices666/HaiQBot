"""媒体库：列表、预览、删除。

`/i/{media_id}` 是**唯一保留下来的旧路由** —— 出站消息里内嵌的图片 URL
指的就是它，换成 SPA 路由会打断已发出去的消息。
"""

from __future__ import annotations

from typing import Any

from starlette.responses import FileResponse, Response

from haiqbot.webui.api.common import Ctx, audit, fail, int_param, ok


def routes(ctx: Ctx) -> list[Any]:
    from starlette.routing import Route

    runtime = ctx.runtime
    media = getattr(runtime, "media", None)

    async def list_media(request: Any) -> Any:
        limit = int_param(request.query_params.get("limit"), 60, low=1, high=200)
        plugin_id = request.query_params.get("plugin") or None
        rows = await runtime.store.media_list(plugin_id=plugin_id, limit=limit)
        for row in rows:
            row["url"] = f"/i/{row['id']}"
        return ok(rows)

    async def file(request: Any) -> Response:
        if media is None:
            return Response(status_code=404)
        media_id = request.path_params["media_id"]
        path = await media.get_path(media_id)
        if path is None:
            return Response(status_code=404)
        mime = await media.get_mime(media_id)
        return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})

    async def delete(request: Any) -> Any:
        media_id = request.path_params["media_id"]
        row = await runtime.store.media_get(media_id)
        if row is None:
            return fail(404, "not_found", f"没有媒体 {media_id}")
        if media is not None and hasattr(media, "delete"):
            removed = await media.delete(media_id)
        else:
            removed = bool(await runtime.store.media_delete(media_id))
        await audit(ctx, "media.delete", {"id": media_id, "ok": removed})
        return ok({"deleted": removed})

    return [
        Route("/api/media", list_media, methods=["GET"]),
        Route("/api/media/{media_id}", delete, methods=["DELETE"]),
        Route("/i/{media_id}", file, methods=["GET"]),
    ]
