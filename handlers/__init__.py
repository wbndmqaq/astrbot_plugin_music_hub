"""路由包：汇总所有模块的路由表。"""

from __future__ import annotations

from .account import routes as account_routes
from .auth import routes as auth_routes
from .base import Route, install
from .detail import routes as detail_routes
from .explore import routes as explore_routes
from .play import routes as play_routes
from .queue import routes as queue_routes
from .share import routes as share_routes
from .subscribe import routes as subscribe_routes
from .system import routes as system_routes


def all_routes() -> list[Route]:
    return [
        *share_routes(),
        *auth_routes(),
        *account_routes(),
        *queue_routes(),
        *subscribe_routes(),
        *system_routes(),
        *explore_routes(),
        *detail_routes(),
        *play_routes(),
    ]


__all__ = ["Route", "install", "all_routes"]
