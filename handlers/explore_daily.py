"""explore 域模块：daily 类发现指令。"""

from __future__ import annotations

import re

from ..core import SOURCE_NAMES, SOURCE_NCM, SOURCE_QQ
from ..core.cards import build_playlist_card_data
from ..core.catalog import (
    FM_FETCHERS,
    HISTORY_DAILY_CLIENT_SOURCE,
    HISTORY_DAILY_DEFAULT_CLIENT_SOURCE,
    HISTORY_DAILY_DEFAULT_HINT,
    HISTORY_DAILY_HINTS,
    NEW_ALBUM_FETCHERS,
    PLAYLIST_RECOMMENDERS,
    RANDOM_FETCHERS,
    RANDOM_PUBLIC_FETCHERS,
    RECOMMEND_CATEGORY_SOURCES,
)
from ..core.errors import ApiError, NotEnabledError
from .explore_common import (
    _RE_BANNER,
    _RE_DAILY,
    _RE_DJ,
    _RE_FM,
    _RE_GUESS,
    _RE_HISTORY_DAILY,
    _RE_HOT,
    _RE_NEW_ALBUM,
    _RE_RANDOM,
    _RE_RECOMMEND,
    _RE_SUGGEST,
    _locked_source,
    _pick_source,
    _send_generic,
    _to_items,
)


async def run_hot_search(service, event):
    m = re.search(_RE_HOT, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    items = await service.call(src, "explore", service.client_of(src).hot_search())
    if not items:
        await service.reply(event, "暂无热搜数据")
        return
    await _send_generic(service, event, f"{SOURCE_NAMES[src]}热搜", items, src)


def _sender_is_admin(event) -> bool:
    """发消息者是否管理员：框架事件的 is_admin()（与注册期 PermissionType.ADMIN
    闸门同一判定来源）。事件缺失该方法（测试桩/适配器）时按普通用户处理。"""
    is_admin = getattr(event, "is_admin", None)
    return bool(is_admin()) if callable(is_admin) else False


async def run_random(service, event):
    # 「来首歌」也是发歌出口：入口补点歌开关与冷却两道闸（此前是唯一无频控的发歌口）。
    # 拒绝时当面提示并吞事件（对齐 play.py 点歌/听N 的口径——只让路不吞会让
    # 群里装的其他音乐插件对同一条消息再回一次，出现双重回复）
    if reason := service.check_song_request():
        await service.reply(event, f"点歌不可用：{reason}")
        return
    if reason := service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    m = re.search(_RE_RANDOM, event.message_str, re.IGNORECASE)
    # 「随机」双轨：管理员走个性化取数（私人电台/日推，即 RANDOM_FETCHERS 现状）；
    # 普通用户走非个性化的新歌榜（RANDOM_PUBLIC_FETCHERS）。个性化表直接取管理员
    # 账号的 personal_fm/everyday_recommend，普通群成员此前可借「随机」公放管理员
    # 的私人电台内容，与 mh_daily/mh_fm 的 admin 闸门政策冲突；本路由对普通用户
    # 保持可用，只隔离画像数据，故在路由内部按身份分轨
    fetchers = RANDOM_FETCHERS if _sender_is_admin(event) else RANDOM_PUBLIC_FETCHERS
    try:
        src = _pick_source(service, event, m.group(1) if m else "")
        # 记账由各平台 fetcher 自己决定（QQ 改动前不记账），故把 service.call 注入进去；
        # _pick_source / client_of 同样可能抛 ApiError（音源未配置），一并纳入退章范围
        songs = await fetchers[src](service.client_of(src), service.call, src)
    except ApiError as e:
        # 已盖冷却章但没播出任何内容：退冷却，不让报错占住点歌间隔（对齐 _song_request）。
        # 走到 play_song 之后的取流失败由下方 except 接住退章，这里不重复退
        service.release_cooldown(event)
        await service.reply(event, e.with_source())
        return
    if not songs:
        service.release_cooldown(event)
        await service.reply(event, "没有拿到随机歌曲，稍后再试")
        return
    import random as _random

    # play_song 裸调失败会逃到路由兜底显示「执行失败，详情见运行日志」：
    # 与 play.py run_play 同口径当面提示（ApiError 才是可转述的上游错误）
    try:
        await service.play_song(event, _random.choice(songs), source_label="随机")
    except ApiError as e:
        service.release_cooldown(event)
        await service.reply(event, e.with_source())


async def run_daily(service, event):
    m = re.search(_RE_DAILY, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    client = service.client_of(src)
    try:
        songs = await service.call(src, "explore", client.daily_recommend())
    except NotEnabledError as e:
        await service.reply(event, e.message)
        return
    except ApiError as e:
        # 只对登录类错误说「需要登录」；超时/风控套这个壳会把排查引向错误方向
        await service.reply(event, e.with_source())
        return
    if not songs:
        await service.reply(event, "今日暂无推荐（可能需要登录）")
        return
    await service.list_to_session(event, "每日推荐", songs, source=src)


async def run_fm(service, event):
    m = re.search(_RE_FM, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    client = service.client_of(src)
    # 私人电台：QQ 侧没有 personal_fm，对应能力是 radar 推荐（返回单曲而非列表）
    result = await service.call(src, "explore", FM_FETCHERS[src](client))
    songs = [result] if isinstance(result, dict) else result
    if not songs:
        # 空结果分两种成因：未登录（电台依赖登录账号）与已登录但暂无推荐——
        # 一刀切「需要登录」会让已登录用户白白去扫码
        try:
            status = await service.call(src, "explore", client.login_status())
        except ApiError:
            status = {}
        if status.get("loggedIn"):
            await service.reply(event, "私人电台暂无推荐内容，稍后再试")
        else:
            await service.reply(event, "私人电台需要登录后使用")
        return
    await service.list_to_session(event, "私人电台", songs, source=src)


async def run_recommend(service, event):
    m = re.search(_RE_RECOMMEND, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    cat = (m.group(2) if m else "").strip()
    client = service.client_of(src)
    pls = await service.call(src, "explore", PLAYLIST_RECOMMENDERS[src](client, cat))
    if not pls:
        await service.reply(event, "暂无推荐歌单")
        return
    from ..core.formatters import format_playlist_text

    # 分类后缀只给真正按分类筛选的平台（当前仅网易云），见 RECOMMEND_CATEGORY_SOURCES
    suffix = f" · {cat}" if cat and src in RECOMMEND_CATEGORY_SOURCES else ""
    data = build_playlist_card_data(
        f"{SOURCE_NAMES[src]}歌单推荐{suffix}",
        "",
        pls,
        source=src,
        tip="回复 歌单 歌单名 查看曲目",
    )
    await service.reply_card_or_text(event, data, "playlist", src, format_playlist_text)


async def run_new_albums(service, event):
    m = re.search(_RE_NEW_ALBUM, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    area = (m.group(2) if m else "").strip()
    client = service.client_of(src)
    albums = await service.call(src, "explore", NEW_ALBUM_FETCHERS[src](client, area))
    if not albums:
        await service.reply(event, "暂无新碟数据")
        return
    await _send_generic(
        service,
        event,
        f"{SOURCE_NAMES[src]}新碟上架" + (f" · {area}" if area else ""),
        _to_items(albums[:15], "album", sub_key="artist"),
        src,
        tip="回复 专辑 专辑名 查看曲目",
    )


async def run_guess(service, event):
    """猜你喜欢（QQ 音源，匿名可用）。"""
    m = re.search(_RE_GUESS, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service,
        event,
        m.group(1) if m else "",
        SOURCE_QQ,
        "猜你喜欢仅支持 QQ 音源（可用 qq: 前缀），网易云/酷狗试试「来首歌」",
    )
    if src is None:
        return
    songs = await service.call(src, "explore", service.client_of(src).guess_songs(10))
    if not songs:
        await service.reply(event, "没有拿到推荐，稍后再试")
        return
    await service.list_to_session(event, "QQ · 猜你喜欢", songs, source=src, tip="回复 听N 播放")


async def run_dj(service, event):
    """精选电台（网易云 /dj/recommend）。"""
    m = re.search(_RE_DJ, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_NCM, "电台推荐仅支持网易云音源（可用 ncm: 前缀）"
    )
    if src is None:
        return
    rows = await service.call(src, "explore", service.client_of(src).dj_radios(10))
    if not rows:
        await service.reply(event, "暂无电台数据")
        return
    await _send_generic(service, event, "网易云 · 精选电台", _to_items(rows, ""), src)


async def run_banner(service, event):
    """首页运营 banner（网易云 /banner），纯展示。"""
    m = re.search(_RE_BANNER, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_NCM, "banner 仅支持网易云音源（可用 ncm: 前缀）"
    )
    if src is None:
        return
    rows = await service.call(src, "explore", service.client_of(src).banners())
    if not rows:
        await service.reply(event, "暂无 banner 数据")
        return
    await _send_generic(service, event, "网易云 · 首页 banner", _to_items(rows, ""), src)


async def run_suggest(service, event):
    """搜索建议：三源联想词并列。"""
    m = re.search(_RE_SUGGEST, event.message_str, re.IGNORECASE)
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：搜索建议 关键词")
        return
    src = _pick_source(service, event, m.group(1) if m else "")
    client = service.client_of(src)
    out = await service.call(src, "explore", client.suggest(kw))
    if not out:
        await service.reply(event, f"「{kw}」没有联想词")
        return
    items = [
        {"index": i + 1, "name": it.get("name", ""), "sub": it.get("sub", ""), "tag": it.get("kind", "")}
        for i, it in enumerate(out)
    ]
    await _send_generic(service, event, f"{SOURCE_NAMES[src]}搜索建议 · {kw}", items, src)


async def run_history_daily(service, event):
    """历史日推（网易云需要黑胶，酷狗需要登录）。"""
    m = re.search(_RE_HISTORY_DAILY, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "", prefer=SOURCE_NCM)
    # 取数客户端与失败提示都按平台查表：改动前是「ncm 走 ncm、其余一律走 kg」，
    # 这里如实保留该行为（含 src 为 qq 时仍取酷狗客户端），见 core/catalog 的说明。
    client_src = HISTORY_DAILY_CLIENT_SOURCE.get(src, HISTORY_DAILY_DEFAULT_CLIENT_SOURCE)
    hint = HISTORY_DAILY_HINTS.get(src, HISTORY_DAILY_DEFAULT_HINT)
    try:
        # 记账按实际取数客户端（client_src）：src=qq 时数据实际来自酷狗，
        # 记在 qq 头上会让统计里的上游调用量失真
        songs = await service.call(client_src, "explore", service.client_of(client_src).history_recommend())
    except ApiError as e:
        await service.reply(event, f"历史日推查询失败：{e.user_msg()}{hint}")
        return
    if not songs:
        await service.reply(event, "暂无历史日推（网易云需要黑胶 VIP，酷狗需要登录）")
        return
    # 标题与卡片主题都按实际取数客户端（client_src）：src=qq 时数据来自酷狗，
    # 沿用 src 会把酷狗数据标成「QQ音乐」/套错卡片配色
    await service.list_to_session(
        event,
        f"历史日推 · {SOURCE_NAMES[client_src]}",
        songs,
        source=client_src,
        tip="回复 听N 播放",
    )
