"""二段式动作实现：歌词 / 逐字歌词 / 评论 / 相似 / MV / 高潮 / 版本。

由 handlers.detail（路由注册）与 handlers.play（听N 分派）调用。
歌词/评论渲染后把全量数据写入 service 分页缓存，供「歌词下页 / 评论下页」翻页。
"""

from __future__ import annotations

import re

from ..core import SOURCE_NCM, SOURCE_QQ
from ..core.cards import build_comment_card_data, build_lyric_card_data
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
    from ..core.formatters import format_lyric_text

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
    # 文本兜底也只发第一页：翻页缓存按 LYRIC_PAGE_SIZE 切页，兜底发全量的话
    # 纯文本平台用户看到的 1-50 行与「歌词下页」翻出的 37-72 行会重叠
    await service.reply_card_or_text(
        event, data, "lyric", song.get("source", ""), lambda d: format_lyric_text(song, shown)
    )


async def _act_lyric_word(service, event, song):
    from ..core.formatters import format_lyric_text

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
    # 与 _act_lyric 同理：文本兜底只发第一页，避免与「歌词下页」的内容重叠
    await service.reply_card_or_text(
        event, data, "lyric", song.get("source", ""), lambda d: format_lyric_text(song, shown)
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
    from ..core.formatters import format_comment_text

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
    # 平台独占能力：三家的「相似歌曲」入口与主键都不同
    # （ncm=simi_songs(sid) / kg=related_songs(sid2 或 sid) / qq=similar_songs(sid2 或 sid)），
    # 且 kg 的 related_songs 不接受 limit 参数。属能力差异，按平台分支。
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
    # 平台独占能力：高潮片段只有酷狗提供（/song/climax），另两家无此接口。
    # 先按平台拒绝再取客户端：顺序反了的话，未配置酷狗时用户看到的是
    # 「音源不可用」而不是「仅支持酷狗」这条更可操作的提示。
    if song.get("source") != "kg":
        await service.reply(event, "高潮片段仅支持酷狗音源（可用 kg: 前缀点歌）")
        return
    client = service.client_of("kg")
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
    # 平台独占能力：只有酷狗与 QQ 提供「其他版本」（kg=related_songs / qq=other_versions），
    # 网易云没有该能力——故按平台分支并对 else 给出明确提示，而不是静默返回空。
    if source == "kg":
        songs = await service.call(
            source, "explore", client.related_songs(song.get("sid2") or song.get("sid", ""))
        )
    elif source == SOURCE_QQ:
        # QQ 的「其他版本」入参是整首歌对象（内部取 mid），与酷狗不同
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
    # 平台独占能力：AI 推荐只有酷狗提供，另两家无此接口，故按平台拒绝。
    # 先拒绝再取客户端，理由同 _act_climax。
    if song.get("source") != "kg":
        await service.reply(event, "AI 推荐仅支持酷狗音源（可用 kg: 前缀点歌）")
        return
    client = service.client_of("kg")
    songs = await service.call("kg", "explore", client.ai_recommend(song.get("sid2") or song.get("sid", "")))
    if not songs:
        await service.reply(event, "没有拿到 AI 推荐")
        return
    await service.list_to_session(
        event, f"AI 推荐 · {song.get('name', '')}", songs, source="kg", tip="回复 听N 播放"
    )


async def _act_simi_playlist(service, event, song):
    """网易云相似歌单：参数必须是歌曲 id。"""
    # 平台独占能力：相似歌单只有网易云提供（/simi/playlist），另两家无此接口。
    # 先拒绝再取客户端，理由同 _act_climax。
    if song.get("source") != SOURCE_NCM:
        await service.reply(event, "相似歌单仅支持网易云音源（可用 ncm: 前缀点歌）")
        return
    client = service.client_of(SOURCE_NCM)
    pls = await service.call(SOURCE_NCM, "explore", client.simi_playlists(song.get("sid", "")))
    if not pls:
        await service.reply(event, "没有找到相似歌单")
        return
    from .explore_common import playlists_as_items

    await service.list_to_session(
        event,
        f"相似歌单 · {song.get('name', '')}",
        playlists_as_items(pls, SOURCE_NCM),
        source=SOURCE_NCM,
        kind="playlists",
        tip="回复 听N 展开对应歌单",
    )
