"""声明式路由安装：把 Route 表挂到 Star 插件类上。

priority 必须挂在**第一个**装饰器上（AstrBot 以 ``__module__+__name__`` 为键，
二次装饰的 kwargs 会被静默丢弃）。admin 路由的 priority 同时传给 permission_type
与 regex/event_message_type 两层：框架只认第一个装饰器的 priority，第二处是
防御性冗余——万一框架行为改为「后者覆盖」，路由优先级也不会静默漂移。

包装器统一做三件事：注册会话（note_umo）、黑白名单校验（check_acl）、
兜底异常回复。ACL 在这里一处生效于全部路由；管理类指令另有
PermissionType(ADMIN) 闸门，但同样受黑白名单约束。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..core.acl import Acl
from ..core.errors import AclDeniedError, ApiError

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

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
    throttle: float = 0.0  # 只读指令轻频控（秒）：explore/账号/二段式类路由设 2.0；0 = 不限


def install(cls, flt, module_path: str, routes: list[Route]) -> int:
    installed = 0
    # 初始化提示按 umo 去重：窗口期同一会话只提示一次，后续命中静默吞事件
    # （首次安装拉依赖的窗口内群聊每条消息都回「初始化中」就是刷屏）。
    # 挂到插件类上：initialize 完成后 main 侧置空释放（窗口期结束该集已无用）
    init_notice_scopes: set[str] = set()
    cls._init_notice_scopes = init_notice_scopes
    for route in routes:
        # admin 路由必须有 pattern 或 event_message_type 之一：admin 只挂
        # permission_type 过滤器（AND 语义的闸门、不是触发器），两者全缺时该路由
        # 没有任何触发器（永不执行）或按框架默认对每条消息执行——装一个必然出错
        # 的路由不如安装期直接失败
        if route.admin and route.pattern is None and route.event_message_type is None:
            raise ValueError(
                f"路由 {route.name} 声明 admin=True 但没有任何触发器（pattern/event_message_type）"
            )

        async def handler(self, event: AstrMessageEvent, _route=route):
            service = self.service
            if service is None:  # initialize 未完成（首次安装拉依赖时窗口不短）
                # 常驻监听类路由（event_message_type=ALL 的链接解析）对每条消息
                # 都命中：窗口期内对全量消息回「初始化中」并 stop_event 等于给
                # 整个群聊刷屏、还连带其他插件失声——静默放行（不回复不吞事件），
                # initialize 完成后自然接管
                if _route.event_message_type is not None:
                    return
                # 指令路由同样要过黑白名单：用插件 config 临时构造 Acl 做同等校验，
                # 否则黑名单用户也会收到「初始化中」且事件被吞。被拒的用户静默吞事件
                # （初始化窗口期连兜底回复能力都不完整，不再回「无权」）；放行的用户
                # 收到初始化提示。原先静默丢弃会让指令被无声吞掉，这里保留「不依赖
                # service 直接回一条提示 + stop_event」的语义
                try:
                    Acl(self.config).check(
                        str(event.get_sender_id() or ""),
                        str(event.get_group_id() or ""),
                        str(event.get_platform_name() or ""),
                    )
                except AclDeniedError:
                    event.stop_event()
                    return
                except Exception:  # noqa: BLE001 - config 缺失等异常按放行处理，保住初始化提示
                    pass
                umo = str(event.unified_msg_origin or "")
                if umo in init_notice_scopes:
                    event.stop_event()
                    return
                init_notice_scopes.add(umo)
                try:
                    await event.send(event.plain_result("音乐插件初始化中，请稍后再试"))
                except Exception:  # noqa: BLE001
                    pass
                event.stop_event()
                return
            taken = True
            try:
                if _route.gated and not service.config.enable:
                    taken = False  # 总开关关闭：静默让路，不回复也不吞事件
                    return False
                service.note_umo(event)
                if _route.check_acl:
                    service.check_acl(event)
                if _route.throttle > 0 and (reason := service.check_browse(event, _route.throttle)):
                    await service.reply(event, reason)
                    return
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
                # 兜底路径没有产出可播放内容（能逃到这里的多是发送链路中途炸掉），
                # 与各 handler 内部的 except 分支同纪律：退还冷却章，不让报错占住点歌间隔。
                # release_cooldown 只退当前事件自己盖的章（见 service.release_cooldown），
                # 非点歌路由的报错不会清掉同群他人正在生效的冷却
                service.release_cooldown(event)
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
