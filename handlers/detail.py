"""歌词 / 评论 / 相似 / MV 等详情路由（二段式：先选歌再执行）。"""

from __future__ import annotations

import re

from ..core.errors import ApiError
from .actions import LYRIC_PAGE_SIZE
from .base import Route

# (pattern, route-name, action, 说明)
_SRC = r"(?:(ncm|kg|qqm|qq)\s*)?"
_RE_LYRIC = rf"^\s*#?{_SRC}歌词\s*(.*?)\s*$"
_RE_LYRIC_WORD = rf"^\s*#?{_SRC}逐字歌词\s*(.*?)\s*$"
_RE_COMMENT = rf"^\s*#?{_SRC}评论\s*(.*?)\s*$"
_RE_SIMILAR = rf"^\s*#?{_SRC}相似\s*(.*?)\s*$"
_RE_SIMI_PLAYLIST = rf"^\s*#?{_SRC}相似歌单\s*(.*?)\s*$"
_RE_MV = rf"^\s*#?{_SRC}(?:MV|mv)\s*(.*?)\s*$"
_RE_CLIMAX = rf"^\s*#?{_SRC}高潮\s*(.*?)\s*$"
_RE_VERSIONS = rf"^\s*#?{_SRC}(?:版本|其他版本)\s*(.*?)\s*$"
_RE_AI = rf"^\s*#?{_SRC}AI推荐\s*(.*?)\s*$"
_RE_LYRIC_PAGE = r"^\s*#?\s*(?:ncm|kg|qqm|qq)?\s*歌词(下页|上页|上一页)\s*$"
_RE_COMMENT_NEXT = r"^\s*#?\s*(?:ncm|kg|qqm|qq)?\s*评论下页\s*$"


async def run_lyric_page(service, event):
    """歌词翻页：数据来自 service 分页缓存（查看歌词/逐字歌词时写入）。"""
    m = re.search(_RE_LYRIC_PAGE, event.message_str, re.IGNORECASE)
    delta = 1 if (m.group(1) if m else "") == "下页" else -1
    scope = service.scope(event)
    data = service.get_pager(scope, "lyric")
    if not data:
        await service.reply(event, "先查看一次歌词（如「歌词 晴天」），再翻页")
        return
    page = data.get("page", 1) + delta
    total = max(1, data.get("total", 1))
    if page < 1:
        await service.reply(event, "已经是第一页啦")
        return
    if page > total:
        await service.reply(event, f"已经是最后一页啦（共 {total} 页）")
        return
    data["page"] = page
    service.set_pager(scope, "lyric", data)
    song = data.get("song", {})
    lines = data.get("lines", [])
    seg = lines[(page - 1) * LYRIC_PAGE_SIZE : page * LYRIC_PAGE_SIZE]
    from ..core.cards import build_lyric_card_data, format_lyric_text

    card = build_lyric_card_data(
        song,
        {},
        seg,
        page=page,
        page_total=total,
        tip=f"第 {page}/{total} 页 · 回复「歌词下页/歌词上页」翻页",
    )
    await service.reply_card_or_text(
        event, card, "lyric", song.get("source", ""), lambda d: format_lyric_text(song, seg)
    )


async def run_comment_next(service, event):
    """评论翻页：第 1 页热门，第 2 页最新（网易云 / QQ）。"""
    scope = service.scope(event)
    data = service.get_pager(scope, "comment")
    if not data:
        await service.reply(event, "先查看一次评论（如「评论 晴天」），再看最新")
        return
    if data.get("page", 1) >= data.get("total", 1):
        await service.reply(event, "没有更多评论啦")
        return
    data["page"] = 2
    service.set_pager(scope, "comment", data)
    song = data.get("song", {})
    comments = data.get("comments", {})
    fresh = comments.get("new") or []
    if not fresh and song.get("source") == "qq":
        # QQ 的最新评论是翻页时现拉的
        sid = song.get("sid2") or song.get("sid", "")
        try:
            fresh = await service.call("qq", "comment", service.qq.new_comments(sid, 12))
        except ApiError as e:
            await service.reply(event, e.with_source())
            return
    if not fresh:
        await service.reply(event, "没有更多评论啦（最新评论仅网易云 / QQ 提供）")
        return
    from ..core.cards import build_comment_card_data, format_comment_text

    view = {"hot": fresh, "total": comments.get("total", 0)}
    card = build_comment_card_data(song, view)
    card["tip"] = "最新评论 · 回复「评论」回看热门"
    await service.reply_card_or_text(
        event, card, "comment", song.get("source", ""), lambda d: format_comment_text(song, view)
    )


def _src_of(token: str | None) -> str:
    from ..core.sources import token_to_source

    return token_to_source(token)


def _make_select_runner(action: str, _actor=None):
    """二段式命令 runner：带关键词 → 搜索+记录 action；不带 → 复用当前列表。"""

    async def run(service, event):
        if reason := service.check_playable():
            await service.reply(event, f"功能不可用：{reason}")
            return
        m = re.search(_PATTERN_OF[action], event.message_str, re.IGNORECASE)
        keyword = (m.group(2) if m else "").strip()
        await service.start_select(event, keyword, action, _ACTION_LABELS[action])

    return run


_PATTERN_OF = {}
_ACTION_LABELS = {
    "lyric": "歌词",
    "lyric_word": "逐字歌词",
    "comment": "热门评论",
    "similar": "相似歌曲",
    "simi_playlist": "相似歌单",
    "mv": "MV",
    "climax": "高潮片段",
    "versions": "其他版本",
    "ai_recommend": "AI 推荐",
}


def routes() -> list[Route]:
    out: list[Route] = [
        # 翻页路由用更高优先级，避免被「歌词 关键词」/「评论 关键词」吃掉
        Route(re.compile(_RE_LYRIC_PAGE), "mh_lyric_page", "歌词翻页", run_lyric_page, priority=7),
        Route(re.compile(_RE_COMMENT_NEXT), "mh_comment_next", "评论翻页", run_comment_next, priority=7),
    ]
    specs = [
        # (pattern, route-name, action, 说明, priority)
        (_RE_LYRIC, "mh_lyric", "lyric", "查看歌词", 6),
        (_RE_LYRIC_WORD, "mh_lyric_word", "lyric_word", "查看逐字歌词", 6),
        (_RE_COMMENT, "mh_comment", "comment", "查看热门评论", 6),
        (_RE_SIMILAR, "mh_similar", "similar", "相似歌曲推荐", 6),
        # 相似歌单 会被「相似 X」前缀吞掉，优先级抬一档
        (_RE_SIMI_PLAYLIST, "mh_simi_playlist", "simi_playlist", "相似歌单（网易云）", 7),
        (_RE_MV, "mh_mv", "mv", "播放歌曲 MV", 6),
        (_RE_CLIMAX, "mh_climax", "climax", "歌曲高潮片段", 6),
        (_RE_VERSIONS, "mh_versions", "versions", "歌曲其他版本", 6),
        (_RE_AI, "mh_ai", "ai_recommend", "AI 相似推荐（酷狗）", 6),
    ]
    for pattern, name, action, doc, prio in specs:
        _PATTERN_OF[action] = pattern
        out.append(
            Route(
                re.compile(pattern, re.IGNORECASE),
                name,
                doc,
                _make_select_runner(action, None),
                priority=prio,
            )
        )
    return out
