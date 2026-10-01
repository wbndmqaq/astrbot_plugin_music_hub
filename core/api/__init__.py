"""音源 API 客户端包：ncm（网易云）/ kg（酷狗）/ qq（QQ音乐）。"""

from __future__ import annotations

from ..errors import ApiError, NotEnabledError
from .http import close_session, get_session
from .kg import KugouClient
from .ncm import NeteaseClient
from .qq import QQClient
from .qq import available as qq_available

__all__ = [
    "ApiError",
    "NotEnabledError",
    "KugouClient",
    "NeteaseClient",
    "QQClient",
    "qq_available",
    "get_session",
    "close_session",
]
