"""explore 域模块：playlist类发现指令。"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM
from ..core.cards import build_playlist_card_data
from ..core.catalog import PLAYLIST_CATEGORY_METHODS, resolver_for
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
    playlists_as_items,
)


async def _resolve_by_keyword(service, event, src: str, kw: str, kind: str, spec: dict) -> None:
    """「关键词 → 专辑/歌单」的统一编排：取 resolver → 调用 → 按结果分支。

    平台差异（三家的方法名、返回结构、唯一/多命中判定）全在 core/catalog 的
    resolver 表里，这里不出现任何平台判断，因此新增平台无需改本函数。
    spec 承载纯展示差异（候选项 kind、tip、两类空结果文案）。
    """
    resolve = resolver_for(kind, src)
    if resolve is None:
        await service.reply(event, spec["unsupported"].format(src=SOURCE_NAMES.get(src, src)))
        return
    res = await resolve(service.client_of(src), kw)
    if res.candidates:
        items = (
            _albums_as_items(res.candidates, src)
            if kind == "album"
            else playlists_as_items(res.candidates, src)
        )
        await service.list_to_session(
            event, f"{spec['label']}候选 · {kw}", items, source=src, kind=spec["kind"], tip=spec["tip"]
        )
        return
    if not res.songs:
        await service.reply(event, spec["not_found"].format(kw=kw) if res.not_found else spec["empty"])
        return
    await service.list_to_session(event, res.title_or(kw), res.songs, source=src)


_ALBUM_SPEC = {
    "label": "专辑",
    "kind": "albums",
    "tip": "回复 听N 展开对应专辑",
    "not_found": "没有找到专辑「{kw}」",
    "empty": "专辑暂无曲目",
    "unsupported": "专辑搜索暂不支持{src}音源",
}
_PLAYLIST_SPEC = {
    "label": "歌单",
    "kind": "playlists",
    "tip": "回复 听N 展开对应歌单",
    "not_found": "没有找到歌单「{kw}」",
    "empty": "歌单暂无曲目",
    "unsupported": "歌单搜索暂不支持{src}音源",
}


async def run_album(service, event):
    m = re.search(_RE_ALBUM, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：专辑 专辑名")
        return
    await _resolve_by_keyword(service, event, src, kw, "album", _ALBUM_SPEC)


async def run_playlist(service, event):
    m = re.search(_RE_PLAYLIST, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "")
    kw = (m.group(2) if m else "").strip()
    if not kw:
        await service.reply(event, "用法：歌单 歌单名 或 歌单 歌单ID")
        return
    if kw.isdigit():
        # 纯 ID 直取：无需搜索，因此复用「听N」展开用的 resolver（同一实现、同一取数上限）
        pl, songs = await resolver_for("playlist_songs", src)(service.client_of(src), kw, kw)
        if not songs:
            await service.reply(event, "歌单暂无曲目或不存在")
            return
        await service.list_to_session(event, pl.get("name") or f"歌单 {kw}", songs, source=src)
        return
    await _resolve_by_keyword(service, event, src, kw, "playlist", _PLAYLIST_SPEC)


async def run_theme(service, event):
    """主题歌单（酷狗）。"""
    m = re.search(_RE_THEME, event.message_str, re.IGNORECASE)
    src = await _locked_source(
        service, event, m.group(1) if m else "", SOURCE_KG, "主题歌单仅支持酷狗音源（可用 kg: 前缀）"
    )
    if src is None:
        return
    pls = await service.call(src, "explore", service.client_of(src).theme_playlists())
    if not pls:
        await service.reply(event, "暂无主题歌单")
        return
    from ..core.formatters import format_playlist_text

    data = build_playlist_card_data("酷狗主题歌单", "", pls, source=src, tip="回复 歌单 歌单名 查看曲目")
    await service.reply_card_or_text(event, data, "playlist", src, format_playlist_text)


async def run_playlist_categories(service, event):
    """歌单分类（网易云 catlist / 酷狗 tags），文本列表。"""
    m = re.search(_RE_PLAYLIST_CATS, event.message_str, re.IGNORECASE)
    src = _pick_source(service, event, m.group(1) if m else "", prefer=SOURCE_NCM)
    client = service.client_of(src)
    # 三个客户端方法名不统一（ncm=playlist_categories / kg=playlist_tags），且 QQ 侧
    # 根本没有这个能力 —— 表里查不到方法名就 getattr 探测，
    # 否则只配了 QQ 的用户（qq 恒 enabled）会直接 AttributeError
    method = PLAYLIST_CATEGORY_METHODS.get(src, "")
    fetch = getattr(client, method, None) if method else None
    if fetch is None:
        await service.reply(
            event, f"歌单分类暂不支持{SOURCE_NAMES.get(src, src)}音源，可用 ncm: 或 kg: 前缀指定"
        )
        return
    cats = await service.call(src, "explore", fetch())
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
    pls = await service.call(src, "explore", service.client_of(src).high_quality_playlists(cat))
    if not pls:
        await service.reply(event, "暂无精品歌单")
        return
    from ..core.formatters import format_playlist_text

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
        cands = await service.call(src, "explore", service.client_of(src).search(kw, 6, 1000))
        if not cands:
            await service.reply(event, f"没有找到歌单「{kw}」")
            return
        if len(cands) > 1:
            await service.list_to_session(
                event,
                f"歌单候选 · {kw}",
                playlists_as_items(cands, src),
                source=src,
                kind="playlists",
                tip="命中多个歌单，回复 听N 展开后用「相关歌单 歌单ID」",
            )
            return
        pid = cands[0]["id"]
    pls = await service.call(src, "explore", service.client_of(src).related_playlists(pid, 10))
    if not pls:
        await service.reply(event, "这个歌单没有相关推荐（官方榜单没有该数据）")
        return
    from ..core.formatters import format_playlist_text

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
    songs = await service.call(src, "explore", service.client_of(src).yueku_songs(30))
    if not songs:
        await service.reply(event, "暂无乐库推荐")
        return
    await service.list_to_session(event, "酷狗 · 乐库推荐", songs, source=src, tip="回复 听N 播放")
