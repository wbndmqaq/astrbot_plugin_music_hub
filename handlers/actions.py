"""二段式动作实现：歌词 / 逐字歌词 / 评论 / 相似 / MV / 高潮 / 版本。

由 handlers.detail（路由注册）与 handlers.play（听N 分派）调用。
歌词/评论渲染后把全量数据写入 service 分页缓存，供「歌词下页 / 评论下页」翻页。
"""

from __future__ import annotations

import re

from ..core import SOURCE_NCM, SOURCE_QQ
from ..core.errors import ApiError

LYRIC_PAGE_SIZE = 36


def _song_meta(song: dict) -> dict:
    return {
        "source": song.get("source", ""),
        "sid": song.get("sid", ""),
        "sid2": song.get("sid2", ""),
        "name": song.get("name", ""),
        "artist": song.get("artist", ""),
        "cover": song.get("cover", ""),
    }


async def _act_lyric(service, event, song):
    from ..core.cards import build_lyric_card_data, format_lyric_text

    try:
        lyric = await service.fetch_lyric(song)
    except ApiError as e:
        await service.reply(event, e.with_source())
        return
    lines = _parse_lrc_lines(lyric.get("lrc", ""))
    trans = _parse_lrc_lines(lyric.get("tlyric", ""))
    if trans and len(trans) <= 12:
        lines = lines + ["── 翻译 ──"] + trans
    if not lines:
        await service.reply(event, "暂无歌词")
        return
    total = max(1, -(-len(lines) // LYRIC_PAGE_SIZE))
    service.set_pager(
        service.scope(event),
        "lyric",
        {"song": _song_meta(song), "lines": lines, "page": 1, "total": total},
    )
    shown = lines[:LYRIC_PAGE_SIZE]
    tip = f"第 1/{total} 页 · 回复「歌词下页」继续" if total > 1 else ""
    data = build_lyric_card_data(song, lyric, shown, page=1, page_total=total, tip=tip)
    await service.reply_card_or_text(
        event, data, "lyric", song.get("source", ""), lambda d: format_lyric_text(song, lines)
    )


async def _act_lyric_word(service, event, song):
    from ..core.cards import build_lyric_card_data, format_lyric_text

    try:
        lyric = await service.fetch_lyric_karaoke(song)
    except ApiError as e:
        await service.reply(event, e.with_source())
        return
    if lyric.get("lines"):
        lines = lyric["lines"]
    elif lyric.get("yrc"):
        lines = _parse_krc_lines(lyric["yrc"]) or _parse_lrc_lines(lyric["yrc"])
    else:
        lines = _parse_lrc_lines(lyric.get("lrc", ""))
    if not lines:
        await service.reply(event, "暂无逐字歌词，试试「歌词」")
        return
    total = max(1, -(-len(lines) // LYRIC_PAGE_SIZE))
    service.set_pager(
        service.scope(event),
        "lyric",
        {"song": _song_meta(song), "lines": lines, "page": 1, "total": total},
    )
    shown = lines[:LYRIC_PAGE_SIZE]
    tip = "逐字歌词" + (f" · 第 1/{total} 页 · 回复「歌词下页」继续" if total > 1 else "")
    data = build_lyric_card_data(song, lyric, shown, page=1, page_total=total, tip=tip)
    await service.reply_card_or_text(
        event, data, "lyric", song.get("source", ""), lambda d: format_lyric_text(song, lines)
    )


def _parse_lrc_lines(lrc: str) -> list[str]:
    from ..core.api.qq import parse_lrc

    try:
        return parse_lrc(lrc)
    except Exception:  # noqa: BLE001
        return []


def _parse_krc_lines(krc: str) -> list[str]:
    """KRC（zlib 解码后的 [毫秒,时长]<逐字标签>文本）→ 行文本。"""

    lines = []
    for raw in (krc or "").splitlines():
        m = re.search(r"^(?:\[\d+,\d+\])?(.*)$", raw)
        body = m.group(1) if m else raw
        txt = re.sub(r"<\d+,\d+,\d+>", "", body).strip()
        if txt:
            lines.append(txt)
    return lines


async def _act_comment(service, event, song):
    from ..core.cards import build_comment_card_data, format_comment_text

    try:
        comments = await service.fetch_comments(song)
    except ApiError as e:
        await service.reply(event, e.with_source())
        return
    if not comments.get("hot"):
        await service.reply(event, "暂无热门评论")
        return
    # 网易云的 comments() 自带最新评论；QQ 的在「评论下页」时再拉
    total = 2 if (comments.get("new") or song.get("source") == SOURCE_QQ) else 1
    service.set_pager(
        service.scope(event),
        "comment",
        {"song": _song_meta(song), "comments": comments, "page": 1, "total": total},
    )
    tip = "第 1/2 页 · 回复「评论下页」看最新评论" if total > 1 else ""
    data = build_comment_card_data(song, comments)
    data["tip"] = tip
    await service.reply_card_or_text(
        event, data, "comment", song.get("source", ""), lambda d: format_comment_text(song, comments)
    )


async def _act_similar(service, event, song):
    source = song.get("source", "")
    client = service.client_of(source)
    if source == SOURCE_NCM:
        songs = await service.call(source, "explore", client.simi_songs(song.get("sid", ""), 10))
    elif source == "kg":
        songs = await service.call(
            source, "explore", client.related_songs(song.get("sid2") or song.get("sid", ""))
        )
    else:
        songs = await service.call(
            source, "explore", client.similar_songs(song.get("sid2") or song.get("sid", ""), 10)
        )
    if not songs:
        await service.reply(event, "没有找到相似歌曲")
        return
    await service.list_to_session(event, f"相似 · {song.get('name', '')}", songs, source=source)


async def _act_mv(service, event, song):
    if not song.get("mvid"):
        await service.reply(event, "这首歌没有关联 MV")
        return
    try:
        r = await service.fetch_mv_url(song)
        url = r.get("url", "")
        if not url:
            await service.reply(event, "MV 链接获取失败")
            return
        from ..core.delivery import deliver_video

        result = await deliver_video(service, event, {"name": song.get("name", "")}, url)
        if not result.get("ok"):
            await service.reply(event, f"MV 发送失败，可手动观看：{url}")
    except ApiError as e:
        await service.reply(event, e.with_source())


async def _act_climax(service, event, song):
    client = service.client_of("kg")
    if song.get("source") != "kg":
        await service.reply(event, "高潮片段仅支持酷狗音源（可用 kg: 前缀点歌）")
        return
    info = await service.call("kg", "explore", client.song_climax(song.get("sid", "")))
    if not info:
        await service.reply(event, "未找到该歌的高潮片段信息")
        return

    def _fmt(ms):
        s = int(ms / 1000)
        return f"{s // 60}:{s % 60:02d}"

    await service.reply(
        event,
        f"♪ {song.get('name')} 高潮片段：{_fmt(info.get('start_ms', 0))} ~ {_fmt(info.get('end_ms', 0))}",
    )


async def _act_versions(service, event, song):
    source = song.get("source", "")
    client = service.client_of(source)
    if source == "kg":
        songs = await service.call(
            source, "explore", client.related_songs(song.get("sid2") or song.get("sid", ""))
        )
    elif source == SOURCE_QQ:
        songs = await service.call(source, "explore", client.other_versions(song))
    else:
        await service.reply(event, "版本查询支持酷狗 / QQ 音源")
        return
    if not songs:
        await service.reply(event, "没有找到其他版本")
        return
    await service.list_to_session(
        event, f"版本 · {song.get('name', '')}", songs, source=source, tip="回复 听N 播放"
    )


async def _act_ai_recommend(service, event, song):
    """酷狗 AI 相似推荐（/ai/recommend，参数是 album_audio_id）。"""
    client = service.client_of("kg")
    if song.get("source") != "kg":
        await service.reply(event, "AI 推荐仅支持酷狗音源（可用 kg: 前缀点歌）")
        return
    songs = await service.call("kg", "explore", client.ai_recommend(song.get("sid2") or song.get("sid", "")))
    if not songs:
        await service.reply(event, "没有拿到 AI 推荐")
        return
    await service.list_to_session(
        event, f"AI 推荐 · {song.get('name', '')}", songs, source="kg", tip="回复 听N 播放"
    )


async def _act_simi_playlist(service, event, song):
    """网易云相似歌单：参数必须是歌曲 id。"""
    client = service.client_of(SOURCE_NCM)
    if song.get("source") != SOURCE_NCM:
        await service.reply(event, "相似歌单仅支持网易云音源（可用 ncm: 前缀点歌）")
        return
    pls = await service.call(SOURCE_NCM, "explore", client.simi_playlists(song.get("sid", "")))
    if not pls:
        await service.reply(event, "没有找到相似歌单")
        return
    from .explore import _playlists_as_items

    await service.list_to_session(
        event,
        f"相似歌单 · {song.get('name', '')}",
        _playlists_as_items(pls, SOURCE_NCM),
        source=SOURCE_NCM,
        kind="playlists",
        tip="回复 听N 展开对应歌单",
    )
