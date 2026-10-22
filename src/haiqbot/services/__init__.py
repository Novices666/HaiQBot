"""内核服务：基础设施（媒体 / 调度）。

生图与对话的 broker 已下沉到各自插件目录：
`plugins/painter/broker.py`、`plugins/chat/broker.py`（插件目录，不随内核发布）。
"""

from .media_http import build_media_app
from .media_store import MediaRef, MediaStore
from .scheduler import Scheduler

__all__ = ["MediaRef", "MediaStore", "Scheduler", "build_media_app"]
