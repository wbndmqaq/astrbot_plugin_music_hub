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
    # 顺序即同优先级仲裁：框架 registry 对同 priority 路由按登记序稳定排序，
    # 重叠指令靠「先登记者先执行」裁决（如 explore/detail 的子命令路由先于 play 的
    # 音源前缀别名）。新增与既有路由重叠的指令时，优先用 priority 拉开次序，
    # 不要依赖这里的排列顺序。
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
