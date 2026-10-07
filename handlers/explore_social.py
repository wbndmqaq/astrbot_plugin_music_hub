"""explore 域模块：social类发现指令。"""

from __future__ import annotations

import re

from ..core import SOURCE_QQ
from ..core.catalog import (
    ALBUM_COMMENT_TYPES,
    ARTIST_ALBUM_FETCHERS,
    PLAYLIST_COMMENT_TYPES,
)
from .explore_common import (
    _RE_ALBUM_COMMENT,
    _RE_ARTIST_ALBUM,
    _RE_PLAYLIST_COMMENT,
    _RE_SIMILAR_SINGER,
    _RE_SINGER_MV,
    _albums_as_items,
    _locked_source,
    _pick_source,
    _send_comments,
    _send_generic,
    _to_items,
)


async def run_album_comments(service, event):
    m = re.search(_RE_ALBUM_COMMENT, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：专辑评论 专辑名")
        return
    client = service.client_of(src)
    # 专辑评论只有网易云 / 酷狗提供：QQ 的评论接口按歌曲 id 走，专辑 id 查不到
    type_ = ALBUM_COMMENT_TYPES.get(src)
    if type_ is None:
        await service.reply(event, "专辑评论支持网易云 / 酷狗音源")
        return
    cands = await service.call(src, "explore", client.search(kw, 5, type_))
    if not cands:
        await service.reply(event, f"没有找到专辑「{kw}」")
        return
    if len(cands) > 1:
        rows = " / ".join(f"{c.get('name')}（{c.get('artist', '')}）" for c in cands[:4])
        await service.reply(event, f"「{kw}」命中多张专辑，请写更具体：{rows}")
        return
    target = cands[0]
    comments = await service.call(src, "comment", client.comments(target["id"], 12, kind="album"))
    if not comments.get("hot"):
        await service.reply(event, "这张专辑暂无热门评论")
        return
    await _send_comments(service, event, target, comments, src)


async def run_playlist_comments(service, event):
    m = re.search(_RE_PLAYLIST_COMMENT, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：歌单评论 歌单名")
        return
    client = service.client_of(src)
    # 歌单评论同样只有网易云 / 酷狗提供（原因同 run_album_comments）
    type_ = PLAYLIST_COMMENT_TYPES.get(src)
    if type_ is None:
        await service.reply(event, "歌单评论支持网易云 / 酷狗音源")
        return
    cands = await service.call(src, "explore", client.search(kw, 5, type_))
    if not cands:
        await service.reply(event, f"没有找到歌单「{kw}」")
        return
    if len(cands) > 1:
        rows = " / ".join(c.get("name", "") for c in cands[:4])
        await service.reply(event, f"「{kw}」命中多个歌单，请写更具体：{rows}")
        return
    target = cands[0]
    comments = await service.call(src, "comment", client.comments(target["id"], 12, kind="playlist"))
    if not comments.get("hot"):
        await service.reply(event, "这个歌单暂无热门评论")
        return
    await _send_comments(service, event, target, comments, src)


async def run_artist_albums(service, event):
    """歌手专辑：ncm / kg / qq 三源。"""
    m = re.search(_RE_ARTIST_ALBUM, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：歌手专辑 歌手名")
        return
    client = service.client_of(src)
    # 三平台都是「搜歌手 → 取其专辑」，差异（搜索类型参数、是否分两步、是否记账）
    # 已在 catalog 内，注入 service.call 让各平台自己决定记账段数
    got = await ARTIST_ALBUM_FETCHERS[src](client, service.call, src, kw)
    if not got.found:
        await service.reply(event, f"没有找到歌手「{kw}」")
        return
    if not got.albums:
        await service.reply(event, f"「{kw}」暂无专辑数据")
        return
    await service.list_to_session(
        event,
        f"歌手专辑 · {kw}",
        _albums_as_items(got.albums, src),
        source=src,
        kind="albums",
        tip="回复 听N 展开对应专辑",
    )


async def run_singer_mvs(service, event):
    """歌手 MV（QQ 音源）。"""
    m = re.search(_RE_SINGER_MV, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_QQ, "歌手 MV 仅支持 QQ 音乐音源（可用 qq: 前缀）"
    )
    if src is None:
        return
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：歌手MV 歌手名")
        return
    singer, mvs = await service.client_of(src).artist_mvs_by_keyword(kw, 10)
    if singer is None or not mvs:
        await service.reply(event, f"没有找到「{kw}」的 MV")
        return
    await service.list_to_session(
        event,
        f"{singer.get('name', kw)} 的 MV",
        _to_items(mvs, "mv", src, sub_key="artist"),
        source=src,
        tip="回复 听N 发送对应 MV",
    )


async def run_similar_singers(service, event):
    """相似歌手（QQ 音源）。"""
    m = re.search(_RE_SIMILAR_SINGER, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_QQ, "相似歌手仅支持 QQ 音乐音源（可用 qq: 前缀）"
    )
    if src is None:
        return
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：相似歌手 歌手名")
        return
    singer, cands = await service.client_of(src).similar_singers_by_keyword(kw, 10)
    if singer is None:
        await service.reply(event, f"没有找到歌手「{kw}」")
        return
    if not cands:
        await service.reply(event, f"没有找到与「{singer.get('name', kw)}」相似的歌手")
        return
    await _send_generic(
        service,
        event,
        f"与「{singer.get('name', kw)}」相似的歌手",
        _to_items(cands, "artist"),
        src,
        tip="回复 歌手 歌手名 听热门歌曲",
    )
