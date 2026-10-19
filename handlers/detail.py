"""歌词 / 评论 / 相似 / MV 等详情路由（二段式：先选歌再执行）。"""

from __future__ import annotations

import re

from ..core import SOURCE_NCM, SOURCE_QQ
from ..core.cards import build_comment_card_data, build_lyric_card_data
from ..core.errors import ApiError
from ..core.sources import token_to_source
from .actions import LYRIC_PAGE_SIZE
from .base import COMMENT_NEXT_HINT, LYRIC_PAGE_HINT, Route

# (pattern, route-name, action, 说明)
_SRC = r"(?:(ncm|kg|qqm|qq)\s*)?"
_RE_LYRIC = rf"^\s*[#＃]?{_SRC}歌词\s*(.*?)\s*$"
_RE_LYRIC_WORD = rf"^\s*[#＃]?{_SRC}逐字歌词\s*(.*?)\s*$"
_RE_COMMENT = rf"^\s*[#＃]?{_SRC}评论\s*(.*?)\s*$"
_RE_SIMILAR = rf"^\s*[#＃]?{_SRC}相似\s*(.*?)\s*$"
_RE_SIMI_PLAYLIST = rf"^\s*[#＃]?{_SRC}相似歌单\s*(.*?)\s*$"
_RE_MV = rf"^\s*[#＃]?{_SRC}(?:MV|mv)\s*(.*?)\s*$"
_RE_CLIMAX = rf"^\s*[#＃]?{_SRC}高潮\s*(.*?)\s*$"
_RE_VERSIONS = rf"^\s*[#＃]?{_SRC}(?:版本|其他版本)\s*(.*?)\s*$"
_RE_AI = rf"^\s*[#＃]?{_SRC}AI\s*推荐\s*(.*?)\s*$"
_RE_LYRIC_PAGE = r"^\s*[#＃]?\s*(?:ncm|kg|qqm|qq)?\s*歌词\s*(下页|上页|上一页)\s*$"
_RE_COMMENT_NEXT = r"^\s*[#＃]?\s*(?:ncm|kg|qqm|qq)?\s*评论\s*下页\s*$"
_RE_COMMENT_PREV = r"^\s*[#＃]?\s*(?:ncm|kg|qqm|qq)?\s*评论\s*(?:上页|上一页)\s*$"


async def run_lyric_page(service, event):
    """歌词翻页：数据来自 service 分页缓存（查看歌词/逐字歌词时写入）。"""
    # 总开关由路由 gated 静默让路，体内不再重复 check_playable
    m = re.search(_RE_LYRIC_PAGE, event.message_str, re.IGNORECASE)
    delta = 1 if (m.group(1) if m else "") == "下页" else -1
    scope = service.scope(event)
    data = service.get_pager(scope, "lyric")
    if not data:
        await service.reply(event, LYRIC_PAGE_HINT)
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
    from ..core.formatters import format_lyric_text

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
        await service.reply(event, COMMENT_NEXT_HINT)
        return
    if data.get("page", 1) >= data.get("total", 1):
        await service.reply(event, "没有更多评论啦")
        return
    song = data.get("song", {})
    comments = data.get("comments", {})
    fresh = comments.get("new") or []
    if not fresh and song.get("source") == SOURCE_QQ:
        # QQ 的最新评论是翻页时现拉的。先取数成功再写 pager：失败就 return 时
        # page 仍是 1，重试「评论下页」不会被判成「没有更多评论」而永久卡死
        sid = song.get("sid2") or song.get("sid", "")
        try:
            fresh = await service.call(
                SOURCE_QQ, "comment", service.client_of(SOURCE_QQ).new_comments(sid, 12)
            )
        except ApiError as e:
            await service.reply(event, e.with_source())
            return
    if not fresh:
        # 空结果两种成因分开说：支持最新评论的平台（网易云 / QQ）是「真的没有了」，
        # 酷狗这类没有该接口的平台是能力边界——混成一句会误导排查
        if song.get("source") in (SOURCE_NCM, SOURCE_QQ):
            await service.reply(event, "没有更多评论啦")
        else:
            await service.reply(event, "最新评论仅网易云 / QQ 提供")
        return
    data["page"] = 2
    service.set_pager(scope, "comment", data)
    from ..core.formatters import format_comment_text

    view = {"hot": fresh, "total": comments.get("total", 0)}
    card = build_comment_card_data(song, view)
    card["tip"] = "最新评论 · 回复「评论上页」回热门 / 「评论下页」继续"
    await service.reply_card_or_text(
        event, card, "comment", song.get("source", ""), lambda d: format_comment_text(song, view)
    )


async def run_comment_prev(service, event):
    """评论上页：从最新评论退回热门首页（pager 的热门数据一直留在缓存 comments.hot）。"""
    scope = service.scope(event)
    data = service.get_pager(scope, "comment")
    if not data:
        await service.reply(event, COMMENT_NEXT_HINT)
        return
    if data.get("page", 1) <= 1:
        await service.reply(event, "已经是第一页啦")
        return
    song = data.get("song", {})
    comments = data.get("comments", {})
    data["page"] = 1
    service.set_pager(scope, "comment", data)
    from ..core.formatters import format_comment_text

    card = build_comment_card_data(song, comments)
    card["tip"] = "热门评论 · 回复「评论下页」看最新"
    await service.reply_card_or_text(
        event, card, "comment", song.get("source", ""), lambda d: format_comment_text(song, comments)
    )


def _make_select_runner(action: str, pattern: str):
    """二段式命令 runner：带关键词 → 搜索+记录 action；不带 → 复用当前列表。

    pattern 由 routes() 直接绑定进闭包（原先经 _PATTERN_OF 查表，存在隐式
    调用时序依赖，独立构造 runner 会 KeyError）。
    """

    async def run(service, event):
        # 总开关由路由 gated 静默让路，体内不再重复 check_playable
        m = re.search(pattern, event.message_str, re.IGNORECASE)
        keyword = (m.group(2) if m else "").strip()
        # 音源前缀（#kg 歌词 晴天）要传给搜索：原先丢弃前缀走三源聚合，
        # 与点歌路由尊重前缀的行为不一致，后续单音源动作也会音源对不上
        prefix = token_to_source(m.group(1) if m else "") if m else "auto"
        await service.start_select(event, keyword, action, _ACTION_LABELS[action], source=prefix)

    return run


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
        Route(
            re.compile(_RE_LYRIC_PAGE, re.IGNORECASE), "mh_lyric_page", "歌词翻页", run_lyric_page, priority=7
        ),
        Route(
            re.compile(_RE_COMMENT_NEXT, re.IGNORECASE),
            "mh_comment_next",
            "评论翻页（最新）",
            run_comment_next,
            priority=7,
        ),
        # 评论上页与下页同优先级：热门口径的数据一直在 pager 缓存里，可直接回退
        Route(
            re.compile(_RE_COMMENT_PREV, re.IGNORECASE),
            "mh_comment_prev",
            "评论翻页（回热门）",
            run_comment_prev,
            priority=7,
        ),
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
        out.append(
            Route(
                re.compile(pattern, re.IGNORECASE),
                name,
                doc,
                _make_select_runner(action, pattern),
                priority=prio,
            )
        )
    # 直刷上游的读指令（歌词/评论/MV 搜索）挂轻频控（与点歌冷却分开计）。
    # 翻页路由例外：读的是分页缓存（仅 QQ 评论下页现拉上游，体内自带限频），
    # 挂 2s 节流会让连续翻页被「操作太频繁」打断
    for r in out:
        r.gated = True
        r.throttle = 0 if r.name in ("mh_lyric_page", "mh_comment_next", "mh_comment_prev") else 2.0
    return out
