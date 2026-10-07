"""分享链接 / 卡片自动解析（全事件监听，优先级最高）。"""

from __future__ import annotations

from astrbot.api.event import filter

from ..core.resolve import HINTS, _looks_like_share, collect_text, handle_resolve, is_plugin_command
from .base import Route


async def run_resolve(service, event):
    if not service.config.enable or not service.config.enable_resolve:
        return
    # resolveCards 只管 JSON 音乐卡片；文本里的分享链接始终解析
    text = collect_text(event, include_json=service.config.resolve_cards)
    if not text or len(text) < 8:
        return
    if is_plugin_command(text):
        return
    # 快速预过滤：必须命中至少一个平台的分享特征
    if not (
        HINTS["ncm"].search(text)
        or HINTS["kg"].search(text)
        or HINTS["qq"].search(text)
        or "qqmusic://" in text
    ):
        return
    # HINTS 只认平台名：闲聊里提一句「酷狗」也能命中预过滤。ACL 校验必须与
    # 冷却/回复一样盖在确认「这真的是一条分享」之后，否则黑名单用户的普通聊天
    # （提到平台名）会被回「无权使用」并吞掉事件
    if not _looks_like_share(text):
        return
    # 本路由常驻监听所有消息，ACL 不能在包装器里查（否则黑名单用户每条消息都被
    # 回复拒绝）；确认消息确实是分享内容、本插件要接管之后才校验
    service.check_acl(event)
    try:
        handled = await handle_resolve(service, event, text)
    except Exception as e:  # noqa: BLE001 - handle_resolve 内部已兜底，这里防御 collect_text 之后的意外
        service.log_warn(f"链接解析路由异常: {e}")
        return
    if handled:
        event.stop_event()


def routes() -> list[Route]:
    return [
        Route(
            None,
            "mh_resolve",
            "解析三平台分享链接",
            run_resolve,
            priority=8,
            event_message_type=filter.EventMessageType.ALL,
            stop_after=False,
            check_acl=False,
        ),
    ]
