"""声明式路由安装：把 Route 表挂到 Star 插件类上。

priority 必须挂在**第一个**装饰器上（AstrBot 以 ``__module__+__name__`` 为键，
二次装饰的 kwargs 会被静默丢弃）。

包装器统一做三件事：注册会话（note_umo）、黑白名单校验（check_acl）、
兜底异常回复。ACL 在这里一处生效于全部路由；管理类指令另有
PermissionType(ADMIN) 闸门，但同样受黑白名单约束。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..core.errors import AclDeniedError, ApiError

# ── 统一文案 ──
# 无列表提示与 core/service.py（start_select 空列表分支）对齐同一句式：
# 那边写着「当前没有歌曲列表，请先「点歌 关键词」或带关键词使用…」，这边只取前半句。
NO_LIST_HINT = "当前没有歌曲列表，请先「点歌 关键词」"
# 翻页/追看的前置提示：还没出过一次内容时先引导查看，例句统一用「晴天」占位
LYRIC_PAGE_HINT = "先查看一次歌词（如「歌词 晴天」），再翻页"
COMMENT_NEXT_HINT = "先查看一次评论（如「评论 晴天」），再看最新"


@dataclass
class Route:
    pattern: str | re.Pattern | None
    name: str
    doc: str
    run: Callable[..., Awaitable]
    admin: bool = False
    priority: int = 0
    event_message_type: Any = None
    stop_after: bool = True  # 处理后是否停止事件传播（点歌等指令应停；解析按需停）
    check_acl: bool = True  # 常驻监听类路由（链接解析）置 False，改在确认接管后自行校验
    gated: bool = False  # 受插件总开关控制：enable=False 时静默让路（登录/帮助/设置类保持可用）


def install(cls, flt, module_path: str, routes: list[Route]) -> int:
    installed = 0
    for route in routes:

        async def handler(self, event, _route=route):
            service = self.service
            if service is None:  # initialize 未完成，静默丢弃（启动竞态窗口极短）
                return
            taken = True
            try:
                if _route.gated and not service.config.enable:
                    taken = False  # 总开关关闭：静默让路，不回复也不吞事件
                    return False
                service.note_umo(event)
                if _route.check_acl:
                    service.check_acl(event)
                taken = await _route.run(service, event) is not False
            except AclDeniedError as e:
                # 黑白名单拒绝：回复一次并终止传播，防止同一条消息被本插件多条路由重复回复
                await service.reply(event, str(e))
                event.stop_event()
            except Exception as e:  # noqa: BLE001 - 兜底：任何路由异常都回复用户而不是静默
                import traceback

                service.log_warn(f"路由 {_route.name} 异常: {e}\n{traceback.format_exc()}")
                # 非 ApiError 的原文可能带内部路径等细节（如 FileNotFoundError），不回群聊
                msg = e.with_source() if isinstance(e, ApiError) else "执行失败，详情见运行日志"
                await service.reply(event, msg)
            finally:
                # run 返回 False 表示路由让路（不属于本插件管的事），保留事件继续传播
                if _route.stop_after and taken:
                    event.stop_event()

        handler.__name__ = route.name
        handler.__qualname__ = f"{cls.__name__}.{route.name}"
        handler.__doc__ = route.doc
        handler.__module__ = module_path

        if route.admin:
            # raise_error=False：无权限时静默跳过本路由并继续传播，不弹"权限不足"、不拦截事件链
            handler = flt.permission_type(
                flt.PermissionType.ADMIN, raise_error=False, priority=route.priority
            )(handler)
        if route.event_message_type is not None:
            handler = flt.event_message_type(route.event_message_type, priority=route.priority)(handler)
        elif route.pattern is not None:
            handler = flt.regex(route.pattern, priority=route.priority)(handler)

        setattr(cls, route.name, handler)
        installed += 1
    return installed
