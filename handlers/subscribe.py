"""订阅推送路由：订阅 日推 / 订阅新歌 歌手名 / 退订 / 我的订阅。

推送在 core.scheduler 每日任务里执行（core.subs.Subscriptions.push_all）。
"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NCM
from ..core.errors import ApiError
from .base import Route

_RE_SUB_DAILY = r"^\s*[#＃]?\s*订阅\s*日推\s*$"
_RE_UNSUB_DAILY = r"^\s*[#＃]?\s*退订\s*日推\s*$"
# 关键词可选：裸「订阅新歌」落到 run_sub_artist 的用法提示
_RE_SUB_ARTIST = r"^\s*[#＃]?\s*订阅新歌(?:\s+(.+?))?\s*$"
_RE_UNSUB_ARTIST = r"^\s*[#＃]?\s*退订新歌(?:\s+(.+?))?\s*$"
_RE_SUB_LIST = r"^\s*[#＃]?\s*(?:我的订阅|订阅列表)\s*$"

_SUB_TIP = "（推送需要平台支持主动消息；群聊/私聊均可订阅）"


def _scheduler_tip(service) -> str:
    """定时任务开关关闭时订阅仍可写入，但推送永远不会执行——必须当面说明，
    不能让用户以为「每天定时推送」还在生效。"""
    if service.config.scheduler_enable:
        return ""
    return "\n注意：定时任务已关闭（scheduler.enable），订阅推送不会执行"


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
    try:
        status = await service.call(SOURCE_NCM, "explore", service.client_of(SOURCE_NCM).login_status())
    except ApiError as e:
        # 登录态查询失败按未登录口径当面提示，与 auth.py 的降级一致，
        # 不让 ApiError 冒泡到路由兜底显示通用文案
        await service.reply(event, f"网易云登录态查询失败：{e.user_msg()}")
        return
    if not status.get("loggedIn"):
        await service.reply(event, "日推需要网易云登录态，请先发送「ncm登录」扫码后再订阅")
        return
    await service.subs.set_daily(umo, service.scope(event), True)
    await service.reply(event, "已订阅每日推荐，每天定时推送到本会话\n" + _SUB_TIP + _scheduler_tip(service))


async def run_unsub_daily(service, event):
    umo = _umo(service, event)
    await service.subs.set_daily(umo, service.scope(event), False)
    await service.reply(event, "已退订每日推荐" + _scheduler_tip(service))


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
    kg_client = service.client_of(SOURCE_KG)
    try:
        kg_status = await kg_client.login_status()
    except ApiError as e:
        await service.reply(event, f"酷狗登录态查询失败：{e.user_msg()}")
        return
    if not kg_status.get("loggedIn"):
        await service.reply(event, "订阅新歌需要在酷狗账号里关注歌手，请先「kg登录」扫码")
        return
    # 搜索也走 service.call 记账：与同函数内 follow_artist 的口径一致
    cands = await service.call(SOURCE_KG, "search", kg_client.search(name, 3, "author"), detail=name)
    if not cands:
        await service.reply(event, f"没有找到歌手「{name}」")
        return
    artist = cands[0]
    # 上游行缺 id/name 时兜底：裸下标的 KeyError 会被路由兜底吃成「执行失败」，
    # name 缺失回落到搜索词（口径对齐 explore_rank.run_rank 的 .get or ""）
    artist_id = str(artist.get("id") or "")
    artist_name = artist.get("name") or name
    if not artist_id:
        await service.reply(event, f"歌手「{name}」的数据缺少 ID（上游格式变化），请换个关键词")
        return
    try:
        # 与其它音源调用一致走 service.call 记账；关注失败就不写订阅，
        # 避免酷狗侧没关注、本地却每天去拉新歌的状态不一致
        await service.call(SOURCE_KG, "explore", kg_client.follow_artist(artist_id, True))
    except ApiError as e:
        await service.reply(event, f"酷狗关注「{artist_name}」失败：{e.user_msg()}，未写入订阅")
        return
    try:
        added = await service.subs.add_artist(
            umo, service.scope(event), {"id": artist_id, "name": artist_name}
        )
    except Exception:  # noqa: BLE001
        # 反向一致性：酷狗侧已关注成功、本地写订阅失败时回滚关注——否则本地无
        # 订阅但账号里多了一个关注，用户重试还会再写一次
        try:
            await service.call(SOURCE_KG, "explore", kg_client.follow_artist(artist_id, False))
        except Exception:  # noqa: BLE001 - 回滚失败只留痕，主错误照常向上抛
            service.log_warn(f"订阅回滚失败：取消关注歌手 {artist_id} 未成功")
        raise
    if added:
        await service.reply(
            event,
            f"已订阅「{artist_name}」的新歌（已在酷狗账号关注），每天检查一次更新\n"
            + _SUB_TIP
            + _scheduler_tip(service),
        )
    else:
        await service.reply(event, f"「{artist_name}」已经在订阅列表里啦")


async def run_unsub_artist(service, event):
    m = re.search(_RE_UNSUB_ARTIST, event.message_str, re.IGNORECASE)
    name = (m.group(1) if m else "").strip()
    if not name:
        await service.reply(event, "用法：退订新歌 歌手名")
        return
    umo = _umo(service, event)
    removed = await service.subs.remove_artist(umo, service.scope(event), name)
    if removed:
        # 已知副作用（有意保留）：这里按订阅记录无条件取关酷狗侧——若管理员本人
        # 在酷狗 App 里也关注了同一歌手（并非经本插件订阅写入），退订会连带取掉
        # 那个关注。不区分来源换取「退订后必定不再推送」的确定性；取关失败已有
        # 容错（不影响本地退订）
        try:
            # 走 service.call 记账：与订阅方向（run_sub_artist）同口径，
            # WebUI 的上游调用量/失败 TOP 才覆盖得到退订路径
            await service.call(
                SOURCE_KG, "explore", service.client_of(SOURCE_KG).follow_artist(removed.get("id", ""), False)
            )
        except Exception:  # noqa: BLE001 - 取关失败不影响退订
            pass
        await service.reply(event, f"已退订「{removed.get('name', name)}」的新歌" + _scheduler_tip(service))
    else:
        await service.reply(event, f"订阅列表里没有「{name}」")


async def run_sub_list(service, event):
    umo = _umo(service, event)
    text = service.subs.describe(umo)
    await service.reply(event, "♪ 我的订阅\n" + text + "\n" + service.subs.source_note())


def routes() -> list[Route]:
    rs = [
        Route(
            re.compile(_RE_SUB_DAILY, re.IGNORECASE),
            "mh_sub_daily",
            "订阅每日推荐（管理员）",
            run_sub_daily,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_UNSUB_DAILY, re.IGNORECASE),
            "mh_unsub_daily",
            "退订每日推荐（管理员）",
            run_unsub_daily,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_SUB_ARTIST, re.IGNORECASE),
            "mh_sub_artist",
            "订阅歌手新歌（管理员）",
            run_sub_artist,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_UNSUB_ARTIST, re.IGNORECASE),
            "mh_unsub_artist",
            "退订歌手新歌（管理员）",
            run_unsub_artist,
            admin=True,
            priority=6,
        ),
        Route(re.compile(_RE_SUB_LIST, re.IGNORECASE), "mh_sub_list", "我的订阅", run_sub_list, priority=6),
    ]
    for r in rs:
        r.gated = True
    # 订阅写入路由（run_sub_daily 查登录态、run_sub_artist 串行打
    # login_status→search→follow_artist 三个上游接口）与 account 类同口径
    # 挂轻频控；「我的订阅」纯读本地 KV，一并从宽挂上保持纪律统一
    for r in rs:
        r.throttle = 2.0
    return rs
