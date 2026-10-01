"""explore 域模块：rank类发现指令。"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM
from .explore_common import (
    _AREA_NEW,
    _QQ_AREA_NEW,
    _RE_ARTIST,
    _RE_HOT_ARTISTS,
    _RE_MV_SEARCH,
    _RE_NEW,
    _RE_RANK,
    _RE_RANK_TOP,
    _RE_TOP_ARTISTS,
    _RE_TOP_CARD,
    _RE_TOP_IP,
    _RE_TOP_MV,
    _locked_source,
    _pick_source,
    _qq_singer_type,
    _send_generic,
    _to_items,
)


async def run_rank(service, event):
    m = re.search(_RE_RANK, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    name = (m.group(2) if m else "").strip()
    client = service.client_of(src)
    if not name:
        lst = await service.call(src, "rank", client.rank_list())
        items = _to_items(lst[:30], "rank", src)
        await service.sessions.set(service.scope(event), "topCategory", {"songs": items, "keyword": "排行榜"})
        await _send_generic(
            service,
            event,
            "排行榜",
            items,
            src,
            tip="回复「排行榜 榜单名」查看具体榜单",
            subtitle="回复 排行榜 榜单名 查看歌曲",
        )
        return
    # 按名匹配
    lst = await service.call(src, "rank", client.rank_list())
    hit = None
    for r in lst:
        if name in (r.get("name") or ""):
            hit = r
            break
    if hit is None:
        for r in lst:
            if (r.get("name") or "") in name:
                hit = r
                break
    if hit is None:
        await service.reply(event, f"没有找到榜单「{name}」，回复「排行榜」查看全部")
        return
    songs = await service.call(src, "rank", client.rank_songs(hit["id"], 30))
    if not songs:
        await service.reply(event, "榜单暂无歌曲")
        return
    await service.list_to_session(event, f"{hit.get('name')}", songs, source=src, tip="回复 听N 播放")


async def run_new_songs(service, event):
    m = re.search(_RE_NEW, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    area = (m.group(2) if m else "").strip()
    client = service.client_of(src)
    if src == SOURCE_NCM:
        songs = await service.call(src, "explore", client.new_songs(_AREA_NEW.get(area, 0)))
    elif src == SOURCE_KG:
        songs = await service.call(src, "explore", client.new_songs(21608))
    else:
        songs = await service.call(src, "explore", client.new_songs(_QQ_AREA_NEW.get(area, 5)))
    if not songs:
        await service.reply(event, "暂无新歌数据")
        return
    await service.list_to_session(event, f"新歌 · {area or '最新'}", songs, source=src)


async def run_artist(service, event):
    m = re.search(_RE_ARTIST, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：歌手 歌手名")
        return
    client = service.client_of(src)
    if src == SOURCE_NCM:
        artist, songs = await client.artist_songs_by_keyword(kw, 30)
    elif src == SOURCE_KG:
        cands = await client.search(kw, 3, "author")
        if not cands:
            await service.reply(event, f"没有找到歌手「{kw}」")
            return
        artist = cands[0]
        songs = await client.artist_songs(artist["id"], 30)
    else:
        artist, songs = await client.artist_songs_by_keyword(kw, 30)
    if not songs:
        await service.reply(event, f"没有找到「{kw}」的歌曲")
        return
    await service.list_to_session(event, f"{artist.get('name', kw)} 的热门歌曲", songs, source=src)


async def run_top_artists(service, event):
    m = re.search(_RE_TOP_ARTISTS, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    client = service.client_of(src)
    if src == SOURCE_NCM:
        artists = await service.call(src, "explore", client.toplist_artist())
    elif src == SOURCE_KG:
        artists = await service.call(src, "explore", client.artist_lists(0))
    else:
        artists = await client.search("热门", 15, _qq_singer_type())
    if not artists:
        await service.reply(event, "暂无歌手榜数据")
        return
    await _send_generic(
        service,
        event,
        f"{SOURCE_NAMES[src]}歌手榜",
        _to_items(artists[:15], "artist"),
        src,
        tip="回复 歌手 歌手名 听热门歌曲",
    )


async def run_hot_artists(service, event):
    """热门歌手（网易云 /top/artists）。"""
    m = re.search(_RE_HOT_ARTISTS, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service,
        event,
        m.group(1) if m else "",
        SOURCE_NCM,
        "热门歌手仅支持网易云音源（可用 ncm: 前缀），歌手榜可试「歌手榜」",
    )
    if src is None:
        return
    artists = await service.call(src, "explore", service.ncm.top_artists())
    if not artists:
        await service.reply(event, "暂无热门歌手数据")
        return
    await _send_generic(
        service,
        event,
        "网易云热门歌手",
        _to_items(artists[:15], "artist"),
        src,
        tip="回复 歌手 歌手名 听热门歌曲",
    )


async def run_mv_search(service, event):
    """MV 搜索：结果入会话，回复 听N 发送对应 MV。"""
    m = re.search(_RE_MV_SEARCH, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：MV搜 关键词")
        return
    client = service.client_of(src)
    mvs = await service.call(src, "explore", client.mv_search(kw, 8))
    if not mvs:
        await service.reply(event, f"没有搜到「{kw}」相关的 MV")
        return
    await service.list_to_session(
        event,
        f"MV · {kw}",
        _to_items(mvs, "mv", src, sub_key="artist"),
        source=src,
        tip="回复 听N 发送对应 MV",
    )


async def run_rank_top(service, event):
    """编辑推荐榜单（酷狗 /rank/top），行结构与排行榜一致可展开。"""
    m = re.search(_RE_RANK_TOP, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_KG, "排行推荐仅支持酷狗音源（可用 kg: 前缀）"
    )
    if src is None:
        return
    rows = await service.call(src, "explore", service.kg.rank_top())
    if not rows:
        await service.reply(event, "暂无排行推荐数据")
        return
    await _send_generic(
        service, event, "酷狗 · 排行推荐", _to_items(rows, "rank", src), src, tip="回复 听N 查看具体榜单"
    )


async def run_top_ip(service, event):
    """编辑精选专题（酷狗 /top/ip），纯展示。"""
    m = re.search(_RE_TOP_IP, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_KG, "编辑精选仅支持酷狗音源（可用 kg: 前缀）"
    )
    if src is None:
        return
    rows = await service.call(src, "explore", service.kg.top_ip())
    if not rows:
        await service.reply(event, "暂无编辑精选数据")
        return
    await _send_generic(service, event, "酷狗 · 编辑精选", _to_items(rows, ""), src)


async def run_top_card(service, event):
    """热门好歌精选（酷狗）。"""
    m = re.search(_RE_TOP_CARD, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_KG, "好歌精选仅支持酷狗音源（可用 kg: 前缀）"
    )
    if src is None:
        return
    songs = await service.call(src, "explore", service.kg.top_card())
    if not songs:
        await service.reply(event, "暂无好歌精选数据")
        return
    await service.list_to_session(event, "酷狗 · 热门好歌精选", songs, source=src, tip="回复 听N 播放")


async def run_top_mvs(service, event):
    """MV 榜（网易云 /top/mv），回复 听N 发送对应 MV。"""
    m = re.search(_RE_TOP_MV, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service,
        event,
        m.group(1) if m else "",
        SOURCE_NCM,
        "MV 榜仅支持网易云音源（可用 ncm: 前缀），找 MV 用「MV搜 关键词」",
    )
    if src is None:
        return
    mvs = await service.call(src, "explore", service.ncm.top_mvs(10))
    if not mvs:
        await service.reply(event, "暂无 MV 榜数据")
        return
    await service.list_to_session(
        event,
        "网易云 · MV 榜",
        _to_items(mvs, "mv", src, sub_key="artist"),
        source=src,
        tip="回复 听N 发送对应 MV",
    )
