"""音源 API 客户端包。

三个平台客户端在各自模块末尾向 registry 注册（``register("ncm", ...)``），
因此本包只需 import 侧模块即可完成注册；实例化走 ``registry.create(config)``，
新增平台不必改本文件。
"""

from __future__ import annotations

from ..errors import ApiError, NotEnabledError

# 导入即注册（顺序无关：各模块末尾的 register 是模块级副作用）
from . import kg as _kg  # noqa: F401,E402
from . import ncm as _ncm  # noqa: F401,E402
from . import qq as _qq  # noqa: F401,E402
from .base import SourceClient, missing_methods
from .http import close_session, get_session
from .kg import KugouClient  # noqa: E402
from .ncm import NeteaseClient  # noqa: E402
from .qq import QQClient  # noqa: E402
from .qq import available as qq_available  # noqa: E402
from .registry import create, register, registered

__all__ = [
    "ApiError",
    "NotEnabledError",
    "SourceClient",
    "KugouClient",
    "NeteaseClient",
    "QQClient",
    "qq_available",
    "get_session",
    "close_session",
    "create",
    "register",
    "registered",
    "missing_methods",
]
