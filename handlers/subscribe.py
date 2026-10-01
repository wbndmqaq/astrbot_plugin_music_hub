"""订阅推送路由：订阅 日推 / 订阅新歌 歌手名 / 退订 / 我的订阅。

推送在 core.scheduler 每日任务里执行（core.subs.SubManager.push_all）。
"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NCM
from ..core.errors import ApiError
from .base import Route

_RE_SUB_DAILY = r"^\s*#?\s*订阅\s*日推\s*$"
_RE_UNSUB_DAILY = r"^\s*#?\s*退订\s*日推\s*$"
_RE_SUB_ARTIST = r"^\s*#?\s*订阅新歌\s+(.+?)\s*$"
_RE_UNSUB_ARTIST = r"^\s*#?\s*退订新歌\s+(.+?)\s*$"
_RE_SUB_LIST = r"^\s*#?\s*(?:我的订阅|订阅列表)\s*$"

_SUB_TIP = "（推送需要平台支持主动消息；群聊/私聊均可订阅）"


def _umo(service, event) -> str:
    umo = getattr(event, "unified_msg_origin", "")
    if not umo:
        raise ApiError("当前会话不支持订阅推送")
    return umo


async def run_sub_daily(service, event):
    umo = _umo(service, event)
    if not service.config.src_enabled(SOURCE_NCM):
        await service.reply(event, "日推订阅需要先配置网易云 API 地址")
        return
    status = await service.ncm.login_status()
    if not status.get("loggedIn"):
        await service.reply(event, "日推需要网易云登录态，请先发送「ncm登录」扫码后再订阅")
        return
    await service.subs.set_daily(umo, service.scope(event), True)
    await service.reply(event, "已订阅每日推荐，每天定时推送到本会话\n" + _SUB_TIP)


async def run_unsub_daily(service, event):
    umo = _umo(service, event)
    await service.subs.set_daily(umo, service.scope(event), False)
    await service.reply(event, "已退订每日推荐")


async def run_sub_artist(service, event):
    m = re.search(_RE_SUB_ARTIST, event.message_str, re.IGNORECASE)
    name = (m.group(1) if m else "").strip()
    if not name:
        await service.reply(event, "用法：订阅新歌 歌手名（酷狗音源）")
        return
    umo = _umo(service, event)
    if not service.config.src_enabled(SOURCE_KG):
        await service.reply(event, "歌手新歌订阅走酷狗音源，请先配置酷狗 API 地址")
        return
    kg_status = await service.kg.login_status()
    if not kg_status.get("loggedIn"):
        await service.reply(event, "订阅新歌需要在酷狗账号里关注歌手，请先「kg登录」扫码")
        return
    cands = await service.kg.search(name, 3, "author")
    if not cands:
        await service.reply(event, f"没有找到歌手「{name}」")
        return
    artist = cands[0]
    await service.kg.follow_artist(artist["id"], True)
    added = await service.subs.add_artist(
        umo, service.scope(event), {"id": artist["id"], "name": artist["name"]}
    )
    if added:
        await service.reply(
            event,
            f"已订阅「{artist['name']}」的新歌（已在酷狗账号关注），每天检查一次更新\n" + _SUB_TIP,
        )
    else:
        await service.reply(event, f"「{artist['name']}」已经在订阅列表里啦")


async def run_unsub_artist(service, event):
    m = re.search(_RE_UNSUB_ARTIST, event.message_str, re.IGNORECASE)
    name = (m.group(1) if m else "").strip()
    if not name:
        await service.reply(event, "用法：退订新歌 歌手名")
        return
    umo = _umo(service, event)
    removed = await service.subs.remove_artist(umo, service.scope(event), name)
    if removed:
        try:
            await service.kg.follow_artist(removed.get("id", ""), False)
        except Exception:  # noqa: BLE001 - 取关失败不影响退订
            pass
        await service.reply(event, f"已退订「{removed.get('name', name)}」的新歌")
    else:
        await service.reply(event, f"订阅列表里没有「{name}」")


async def run_sub_list(service, event):
    umo = _umo(service, event)
    text = service.subs.describe(umo)
    await service.reply(event, "♪ 我的订阅\n" + text + "\n" + service.subs.source_note())


def routes() -> list[Route]:
    rs = [
        Route(
            re.compile(_RE_SUB_DAILY), "mh_sub_daily", "订阅每日推荐", run_sub_daily, admin=True, priority=6
        ),
        Route(
            re.compile(_RE_UNSUB_DAILY),
            "mh_unsub_daily",
            "退订每日推荐",
            run_unsub_daily,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_SUB_ARTIST),
            "mh_sub_artist",
            "订阅歌手新歌",
            run_sub_artist,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_UNSUB_ARTIST),
            "mh_unsub_artist",
            "退订歌手新歌",
            run_unsub_artist,
            admin=True,
            priority=6,
        ),
        Route(re.compile(_RE_SUB_LIST), "mh_sub_list", "我的订阅", run_sub_list, priority=6),
    ]
    for r in rs:
        r.gated = True
    return rs
