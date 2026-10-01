"""explore 域模块：playlist类发现指令。"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM
from .explore_common import (
    _RE_ALBUM,
    _RE_HIGHQUALITY,
    _RE_PLAYLIST,
    _RE_PLAYLIST_CATS,
    _RE_RELATED_PLAYLIST,
    _RE_THEME,
    _RE_YUEKU,
    _albums_as_items,
    _locked_source,
    _pick_source,
    _playlists_as_items,
)


async def run_album(service, event):
    m = re.search(_RE_ALBUM, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：专辑 专辑名")
        return
    client = service.client_of(src)
    if src == SOURCE_NCM:
        cands = await client.search(kw, 6, type_=10)
        if not cands:
            await service.reply(event, f"没有找到专辑「{kw}」")
            return
        if len(cands) == 1:
            detail = await client.album_detail(cands[0]["id"])
            songs = detail.get("songs", [])
            meta = detail.get("album") or cands[0]
        else:
            await service.list_to_session(
                event,
                f"专辑候选 · {kw}",
                _albums_as_items(cands, src),
                source=src,
                kind="albums",
                tip="回复 听N 展开对应专辑",
            )
            return
    elif src == SOURCE_KG:
        cands = await client.search(kw, 6, "album")
        if not cands:
            await service.reply(event, f"没有找到专辑「{kw}」")
            return
        if len(cands) == 1:
            meta = cands[0]
            songs = await client.album_songs(meta["id"], 30)
        else:
            await service.list_to_session(
                event,
                f"专辑候选 · {kw}",
                _albums_as_items(cands, src),
                source=src,
                kind="albums",
                tip="回复 听N 展开对应专辑",
            )
            return
    else:
        meta, songs = await client.album_songs_by_keyword(kw, 30)
        if meta is None and songs and "songCount" in songs[0]:
            await service.list_to_session(
                event,
                f"专辑候选 · {kw}",
                _albums_as_items(songs, src),
                source=src,
                kind="albums",
                tip="回复 听N 展开对应专辑",
            )
            return
    if not songs:
        await service.reply(event, "专辑暂无曲目")
        return
    await service.list_to_session(event, f"{meta.get('name', kw)}", songs, source=src)


async def run_playlist(service, event):
    m = re.search(_RE_PLAYLIST, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：歌单 歌单名 或 歌单 歌单ID")
        return
    client = service.client_of(src)
    if kw.isdigit():
        # 直接按 ID 取
        if src == SOURCE_NCM:
            pl, songs = await service.ncm_playlist_songs(kw)
        elif src == SOURCE_KG:
            songs, name, cover = await client.playlist_songs(kw, 30)
            pl = {"name": name, "cover": cover}
        else:
            meta, songs = await client.songlist_songs(kw, 30)
            pl = meta
        if not songs:
            await service.reply(event, "歌单暂无曲目或不存在")
            return
        await service.list_to_session(event, pl.get("name") or f"歌单 {kw}", songs, source=src)
        return
    if src == SOURCE_NCM:
        meta, songs = await client.playlist_songs_by_keyword(kw, 30)
        if meta is None and songs:
            await service.list_to_session(
                event,
                f"歌单候选 · {kw}",
                _playlists_as_items(songs, src),
                source=src,
                kind="playlists",
                tip="回复 听N 展开对应歌单",
            )
            return
    elif src == SOURCE_KG:
        cands = await client.search(kw, 6, "special")
        if not cands:
            await service.reply(event, f"没有找到歌单「{kw}」")
            return
        if len(cands) == 1:
            songs, name, cover = await client.playlist_songs(cands[0]["id"], 30)
            meta = {"name": name or cands[0].get("name", "")}
        else:
            await service.list_to_session(
                event,
                f"歌单候选 · {kw}",
                _playlists_as_items(cands, src),
                source=src,
                kind="playlists",
                tip="回复 听N 展开对应歌单",
            )
            return
    else:
        meta, songs = await client.songlist_by_keyword(kw, 30)
        if meta is None and songs and "trackCount" in songs[0]:
            await service.list_to_session(
                event,
                f"歌单候选 · {kw}",
                _playlists_as_items(songs, src),
                source=src,
                kind="playlists",
                tip="回复 听N 展开对应歌单",
            )
            return
    if not songs:
        await service.reply(event, "歌单暂无曲目")
        return
    await service.list_to_session(
        event, meta.get("name", kw) if isinstance(meta, dict) else kw, songs, source=src
    )


async def run_theme(service, event):
    """主题歌单（酷狗）。"""
    m = re.search(_RE_THEME, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_KG, "主题歌单仅支持酷狗音源（可用 kg: 前缀）"
    )
    if src is None:
        return
    pls = await service.call(src, "explore", service.kg.theme_playlists())
    if not pls:
        await service.reply(event, "暂无主题歌单")
        return
    from ..core.cards import build_playlist_card_data, format_playlist_text

    data = build_playlist_card_data("酷狗主题歌单", "", pls, source=src, tip="回复 歌单 歌单名 查看曲目")
    await service.reply_card_or_text(event, data, "playlist", src, format_playlist_text)


async def run_playlist_categories(service, event):
    """歌单分类（网易云 catlist / 酷狗 tags），文本列表。"""
    m = re.search(_RE_PLAYLIST_CATS, event.message_str, re.IGNORECASE)
    src = await _pick_source(service, event, m.group(1) if m else "", prefer=SOURCE_NCM)
    client = service.client_of(src)
    cats = await service.call(
        src, "explore", client.playlist_categories() if src == SOURCE_NCM else client.playlist_tags()
    )
    if not cats:
        await service.reply(event, "暂无分类数据")
        return
    lines = [f"♪ {SOURCE_NAMES[src]}歌单分类（{len(cats)} 个）", " / ".join(cats)]
    lines.append("用法：歌单推荐 分类名")
    await service.reply(event, "\n".join(lines))


async def run_highquality(service, event):
    """精品歌单（网易云 /top/playlist/highquality）。"""
    m = re.search(_RE_HIGHQUALITY, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service,
        event,
        m.group(1) if m else "",
        SOURCE_NCM,
        "精品歌单仅支持网易云音源（可用 ncm: 前缀），酷狗有「好歌精选」",
    )
    if src is None:
        return
    cat = (m.group(2) if m else "").strip()
    pls = await service.call(src, "explore", service.ncm.high_quality_playlists(cat))
    if not pls:
        await service.reply(event, "暂无精品歌单")
        return
    from ..core.cards import build_playlist_card_data, format_playlist_text

    data = build_playlist_card_data(
        "网易云 · 精品歌单" + (f" · {cat}" if cat else ""),
        "",
        pls,
        source=src,
        tip="回复 歌单 歌单名 查看曲目",
    )
    await service.reply_card_or_text(event, data, "playlist", src, format_playlist_text)


async def run_related_playlists(service, event):
    """相关歌单（网易云 rcmd）：歌单 ID 直取，关键词先定位歌单。"""
    m = re.search(_RE_RELATED_PLAYLIST, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_NCM, "相关歌单仅支持网易云音源（可用 ncm: 前缀）"
    )
    if src is None:
        return
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：相关歌单 歌单名 或 相关歌单 歌单ID")
        return
    if kw.isdigit():
        pid = kw
    else:
        cands = await service.call(src, "explore", service.ncm.search(kw, 6, 1000))
        if not cands:
            await service.reply(event, f"没有找到歌单「{kw}」")
            return
        if len(cands) > 1:
            await service.list_to_session(
                event,
                f"歌单候选 · {kw}",
                _playlists_as_items(cands, src),
                source=src,
                kind="playlists",
                tip="命中多个歌单，回复 听N 展开后用「相关歌单 歌单ID」",
            )
            return
        pid = cands[0]["id"]
    pls = await service.call(src, "explore", service.ncm.related_playlists(pid, 10))
    if not pls:
        await service.reply(event, "这个歌单没有相关推荐（官方榜单没有该数据）")
        return
    from ..core.cards import build_playlist_card_data, format_playlist_text

    data = build_playlist_card_data(
        "网易云 · 相关歌单推荐", "", pls, source=src, tip="回复 歌单 歌单名 查看曲目"
    )
    await service.reply_card_or_text(event, data, "playlist", src, format_playlist_text)


async def run_yueku(service, event):
    """乐库推荐（酷狗 /yueku），回复 听N 播放。"""
    m = re.search(_RE_YUEKU, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_KG, "乐库仅支持酷狗音源（可用 kg: 前缀）"
    )
    if src is None:
        return
    songs = await service.call(src, "explore", service.kg.yueku_songs(30))
    if not songs:
        await service.reply(event, "暂无乐库推荐")
        return
    await service.list_to_session(event, "酷狗 · 乐库推荐", songs, source=src, tip="回复 听N 播放")
