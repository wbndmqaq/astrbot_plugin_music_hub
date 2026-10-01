"""explore 域模块：daily类发现指令。"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM, SOURCE_QQ
from ..core.errors import ApiError
from .explore_common import (
    _AREA_NCM_ALBUM,
    _KG_AREA_ALBUM,
    _QQ_AREA_ALBUM,
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
    _kg_new_albums,
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
    client = service.client_of(src)
    if src == SOURCE_NCM:
        songs = await service.call(src, "explore", client.personal_fm())
        if not songs:
            songs = await service.call(src, "explore", client.personalized_newsong())
    elif src == SOURCE_KG:
        songs = await service.call(src, "explore", client.personal_fm())
        if not songs:
            songs = await service.call(src, "explore", client.everyday_recommend())
    else:
        song = await client.random_song()
        songs = [song] if song else []
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
    songs = await service.call(src, "explore", client.personal_fm())
    if not songs:
        await service.reply(event, "私人电台需要登录后使用")
        return
    await service.list_to_session(event, "私人电台", songs, source=src)


async def run_recommend(service, event):
    m = re.search(_RE_RECOMMEND, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    cat = (m.group(2) if m else "").strip()
    client = service.client_of(src)
    if src == SOURCE_NCM:
        pls = (
            await service.call(src, "explore", client.top_playlists(cat))
            if cat
            else await service.call(src, "explore", client.personalized(15))
        )
    elif src == SOURCE_KG:
        pls = await service.call(src, "explore", client.top_playlists())
    else:
        pls = await service.call(src, "explore", client.recommend_playlists())
    if not pls:
        await service.reply(event, "暂无推荐歌单")
        return
    from ..core.cards import build_playlist_card_data, format_playlist_text

    data = build_playlist_card_data(
        f"{SOURCE_NAMES[src]}歌单推荐" + (f" · {cat}" if cat and src == SOURCE_NCM else ""),
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
    if src == SOURCE_NCM:
        # 带地区走新碟榜，不带走最新上架
        albums = (
            await service.call(src, "explore", client.top_albums(_AREA_NCM_ALBUM[area]))
            if area in _AREA_NCM_ALBUM
            else await service.call(src, "explore", client.album_newest())
        )
    elif src == SOURCE_KG:
        albums = await service.call(src, "explore", _kg_new_albums(client, _KG_AREA_ALBUM.get(area, 0)))
    else:
        albums = await service.call(src, "explore", client.new_albums(_QQ_AREA_ALBUM.get(area, 1), 15))
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
    songs = await service.call(src, "explore", service.qq.guess_songs(10))
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
    rows = await service.call(src, "explore", service.ncm.dj_radios(10))
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
    rows = await service.call(src, "explore", service.ncm.banners())
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
    if src == SOURCE_NCM:
        try:
            songs = await service.call(src, "explore", service.ncm.history_recommend())
        except ApiError as e:
            await service.reply(event, f"历史日推查询失败：{e.user_msg()}（黑胶特权）")
            return
    else:
        try:
            songs = await service.call(src, "explore", service.kg.history_recommend())
        except ApiError as e:
            await service.reply(event, f"历史日推查询失败：{e.user_msg()}（需酷狗登录）")
            return
    if not songs:
        await service.reply(event, "暂无历史日推（网易云需要黑胶 VIP，酷狗需要登录）")
        return
    await service.list_to_session(
        event, f"历史日推 · {SOURCE_NAMES[src]}", songs, source=src, tip="回复 听N 播放"
    )
