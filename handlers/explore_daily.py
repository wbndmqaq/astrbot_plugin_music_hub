"""explore 域模块：daily类发现指令。"""

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
    RECOMMEND_CATEGORY_SOURCES,
)
from ..core.errors import ApiError
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
    src = await _pick_source(service, event, m.group(1) if m else "")
    items = await service.call(src, "explore", service.client_of(src).hot_search())
    if not items:
        await service.reply(event, "暂无热搜数据")
        return
    await _send_generic(service, event, f"{SOURCE_NAMES[src]}热搜", items, src)


async def run_random(service, event):
    m = re.search(_RE_RANDOM, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    # 记账由各平台 fetcher 自己决定（QQ 改动前不记账），故把 service.call 注入进去
    songs = await RANDOM_FETCHERS[src](service.client_of(src), service.call, src)
    if not songs:
        await service.reply(event, "没有拿到随机歌曲，稍后再试")
        return
    import random as _random

    await service.play_song(event, _random.choice(songs), source_label="随机")


async def run_daily(service, event):
    m = re.search(_RE_DAILY, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    client = service.client_of(src)
    try:
        songs = await service.call(src, "explore", client.daily_recommend())
    except ApiError as e:
        await service.reply(event, f"日推需要登录：{e.user_msg()}")
        return
    if not songs:
        await service.reply(event, "今日暂无推荐（可能需要登录）")
        return
    await service.list_to_session(event, "每日推荐", songs, source=src)


async def run_fm(service, event):
    m = re.search(_RE_FM, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    client = service.client_of(src)
    # 私人电台：QQ 侧没有 personal_fm，对应能力是 radar 推荐（返回单曲而非列表）
    result = await service.call(src, "explore", FM_FETCHERS[src](client))
    songs = [result] if isinstance(result, dict) else result
    if not songs:
        await service.reply(event, "私人电台需要登录后使用")
        return
    await service.list_to_session(event, "私人电台", songs, source=src)


async def run_recommend(service, event):
    m = re.search(_RE_RECOMMEND, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
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
    src = await _pick_source(service, event, m.group(1) if m else "")
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
    src = await _pick_source(service, event, m.group(1) if m else "")
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
    src = await _pick_source(service, event, m.group(1) if m else "", prefer=SOURCE_NCM)
    # 取数客户端与失败提示都按平台查表：改动前是「ncm 走 ncm、其余一律走 kg」，
    # 这里如实保留该行为（含 src 为 qq 时仍取酷狗客户端），见 core/catalog 的说明。
    client_src = HISTORY_DAILY_CLIENT_SOURCE.get(src, HISTORY_DAILY_DEFAULT_CLIENT_SOURCE)
    hint = HISTORY_DAILY_HINTS.get(src, HISTORY_DAILY_DEFAULT_HINT)
    try:
        songs = await service.call(src, "explore", service.client_of(client_src).history_recommend())
    except ApiError as e:
        await service.reply(event, f"历史日推查询失败：{e.user_msg()}{hint}")
        return
    if not songs:
        await service.reply(event, "暂无历史日推（网易云需要黑胶 VIP，酷狗需要登录）")
        return
    await service.list_to_session(
        event, f"历史日推 · {SOURCE_NAMES[src]}", songs, source=src, tip="回复 听N 播放"
    )
