"""点歌 / 播放路由（聚合 + 三音源前缀别名）。"""

from __future__ import annotations

import re

from ..core.cards import build_versions_card_data
from ..core.errors import ApiError
from ..core.formatters import format_versions_text
from ..core.sources import token_to_source, word_to_source
from .actions import (  # noqa: F401
    _act_ai_recommend,
    _act_climax,
    _act_comment,
    _act_lyric,
    _act_lyric_word,
    _act_mv,
    _act_simi_playlist,
    _act_similar,
    _act_versions,
)
from .base import Route

# 统一入口
RE_REQUEST = r"^\s*#?\s*(?:点歌|音乐|music|mh)\s+(.+?)\s*$"
RE_PLAY = r"^\s*#?\s*(?:播放|play)\s+(.+?)\s*$"
RE_LISTEN_N = r"^\s*#?\s*听\s*(\d{1,3})\s*(?:首)?\s*(ncm|kg|qq|网|狗|q)?\s*$"
RE_LISTEN_ALL = r"^\s*#?\s*听\s*所?\s*有\s*(?:首)?\s*$"
RE_SWITCH = r"^\s*#?\s*(?:换源|音源)\s*(ncm|wy|kg|kugou|qq|qqm|网易云|酷狗|qq音乐)\s*$"
# 音源前缀别名：#ncm点歌 晴天 / #ncm 晴天 / #kg点歌 晴天 …
# 负向断言排除子命令词（#ncm 歌词 xx 归歌词路由处理）。新增子命令时在这里补词，
# 也要同步 handlers 里对应的正则。
_SRC_CMD_LOOKAHEAD = (
    r"(?!(?:\S*\s*)?(?:点歌|歌词|逐字|评论|相似|高潮|版本|MV|mv|排行|新歌|歌手|专辑|歌单|"
    r"热搜|来首|日推|FM|fm|推荐|新碟|歌手榜|状态|登录|登出|帮助|设置|音质|api|统计|测试|听|音源|换源|"
    r"喜欢|红心|点赞|取消|云盘|已购|最近|历史|关注列表|"
    r"AI推荐|编辑精选|乐库|电台|猜你喜欢|搜索建议|banner))"
)
RE_SRC_REQUEST = rf"^\s*#?(ncm|kg|qqm|qq)\s*(?:点歌)?\s+{_SRC_CMD_LOOKAHEAD}(.+?)\s*$"


def _src_token(token: str | None) -> str:
    """听N 的音源后缀（网/狗/q…）。"""
    return token_to_source(token, default="")


async def run_switch(service, event):
    """换源重放：把最近播放的歌在指定音源重新找来播。"""
    reason = service.check_song_request()
    if reason:
        await service.reply(event, f"点歌不可用：{reason}")
        return
    m = re.search(RE_SWITCH, event.message_str, re.IGNORECASE)
    if not m:
        return
    src = word_to_source(m.group(1))
    if not src:
        await service.reply(event, "用法：换源 ncm / 换源 kg / 换源 qq")
        return
    if not service.config.src_enabled(src):
        names = {"ncm": "网易云", "kg": "酷狗", "qq": "QQ音乐"}
        await service.reply(event, f"{names[src]}音源未启用")
        return
    if reason := await service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    try:
        await service.switch_source(event, src)
    except ApiError as e:
        service.release_cooldown(event)
        await service.reply(event, e.with_source())


def _map_source(token: str) -> str:
    return token_to_source(token)


async def _no_result_hint(service, event, keyword: str, notices: list[str] | None = None) -> None:
    """零结果回复：优先告知"某音源需扫码"，其次给搜索建议。

    notices 由本次搜索随结果一起返回——不能从 search 实例上取，
    那样并发搜索会互相取走对方的提示。
    """
    if notices:
        await service.reply(event, f"没有搜到「{keyword}」。\n" + "\n".join(notices))
        return
    sug = await service.suggest_for(keyword)
    if sug:
        await service.reply(event, f"没有搜到「{keyword}」。你是不是想搜：{' / '.join(sug[:5])}")
    else:
        await service.reply(event, f"没有搜到「{keyword}」相关的歌曲")


async def _song_request(service, event, keyword: str, forced_source: str = "auto"):
    reason = service.check_song_request()
    if reason:
        await service.reply(event, f"点歌不可用：{reason}")
        return
    keyword = (keyword or "").strip()
    if not keyword:
        await service.reply(event, "用法：点歌 关键词（ncm:/kg:/qq: 前缀可指定音源）")
        return
    if reason := await service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    if forced_source == "auto":
        # 配置了具体默认音源就单源搜索；auto 才走三平台聚合
        forced_source = service.config.default_source
    if forced_source != "auto":
        songs, src, notices = await service.search_with_notices(keyword, forced_source)
        if not songs:
            service.release_cooldown(event)
            await _no_result_hint(service, event, keyword, notices)
            return
        tip = f"共 {len(songs)} 首"
        await service.list_to_session(event, keyword, songs, source=src, tip=tip)
        return
    # 聚合模式：同名同歌手合并为一行，呈现多音源选择
    groups, src = await service.search_versions(keyword)
    if not groups:
        service.release_cooldown(event)
        await _no_result_hint(service, event, keyword)
        return
    scope = service.scope(event)
    await service.sessions.set(scope, "versions", {"keyword": keyword, "songs": groups})
    data = build_versions_card_data(keyword, groups, tip=f"共 {len(groups)} 首 · 听N 播放 / 听N qq 指定音源")
    await service.reply_card_or_text(event, data, "list", "auto", format_versions_text)


async def run_request(service, event):
    m = re.search(RE_REQUEST, event.message_str, re.IGNORECASE)
    await _song_request(service, event, m.group(1) if m else "")


async def run_src_request(service, event):
    m = re.search(RE_SRC_REQUEST, event.message_str, re.IGNORECASE)
    if not m:
        return
    await _song_request(service, event, m.group(2), _map_source(m.group(1)))


async def run_play(service, event):
    reason = service.check_song_request()
    if reason:
        await service.reply(event, f"点歌不可用：{reason}")
        return
    m = re.search(RE_PLAY, event.message_str, re.IGNORECASE)
    keyword = m.group(1).strip() if m else ""
    if not keyword:
        return
    if reason := await service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    songs, _ = await service.search_songs(keyword, service.config.default_source, limit=1)
    if not songs:
        service.release_cooldown(event)
        await service.reply(event, f"没有搜到「{keyword}」相关的歌曲")
        return
    await service.play_song(event, songs[0])


async def run_listen_n(service, event):
    reason = service.check_song_request()
    if reason:
        return False  # 功能关闭：静默让路，不吞事件
    m = re.search(RE_LISTEN_N, event.message_str, re.IGNORECASE)
    if not m:
        return
    n = int(m.group(1))
    want_src = _src_token(m.group(2))
    scope = service.scope(event)
    # 会话仲裁：别的音乐插件刚出过列表时本插件不抢答（裸 听N）
    if not await service.sessions.owns_scope(scope):
        return False
    song, action = await service.take_action_target(event, n)
    if song is None:
        return
    # 多音源分组里指定音源：take_action_target 返回的是分组
    if song.get("versions"):
        try:
            await service.play_group(event, song, source=want_src)
        except ApiError as e:
            await service.reply(event, e.with_source())
        return
    if want_src and not song.get("kind"):
        # 普通列表 + 指定音源：按歌名歌手在该音源重搜
        try:
            await service.switch_source_for(event, song, want_src)
        except ApiError as e:
            await service.reply(event, e.with_source())
        return
    # 候选项展开（专辑/歌单/榜单分类/MV 候选）
    if song.get("kind") in ("album", "playlist", "rank", "mv"):
        from .explore_common import expand_candidate

        try:
            handled = await expand_candidate(service, event, song, n)
        except ApiError as e:
            await service.reply(event, e.with_source())
            return
        if not handled:
            await service.reply(event, "该候选项没有可展开的内容")
        return
    if action == "lyric":
        await _act_lyric(service, event, song)
    elif action == "lyric_word":
        await _act_lyric_word(service, event, song)
    elif action == "comment":
        await _act_comment(service, event, song)
    elif action == "similar":
        await _act_similar(service, event, song)
    elif action == "simi_playlist":
        await _act_simi_playlist(service, event, song)
    elif action == "mv":
        await _act_mv(service, event, song)
    elif action == "climax":
        await _act_climax(service, event, song)
    elif action == "versions":
        await _act_versions(service, event, song)
    elif action == "ai_recommend":
        await _act_ai_recommend(service, event, song)
    else:
        await service.play_song(event, song)


async def run_listen_all(service, event):
    reason = service.check_song_request()
    if reason:
        return False  # 功能关闭：静默让路，与 run_listen_n 一致
    scope = service.scope(event)
    if not await service.sessions.owns_scope(scope):
        return False  # 列表是别的音乐插件出的：让路不抢答
    songs = await service.sessions.songs_of(scope)
    if not songs:
        await service.reply(event, "当前没有歌曲列表，先「点歌 关键词」吧")
        return
    await service.play_all(event, songs)


# ──────────── 路由表 ────────────
def routes() -> list[Route]:
    return [
        # 优先级 6：音源前缀别名先于统一入口（#ncm点歌 不会被统一入口抢走）
        Route(
            re.compile(RE_SRC_REQUEST, re.IGNORECASE),
            "mh_src_request",
            "指定音源点歌",
            run_src_request,
            priority=6,
        ),
        Route(
            re.compile(RE_REQUEST, re.IGNORECASE),
            "mh_request",
            "点歌（默认音源可配）",
            run_request,
            priority=5,
        ),
        Route(re.compile(RE_PLAY, re.IGNORECASE), "mh_play", "直接播放", run_play, priority=5),
        Route(
            re.compile(RE_LISTEN_ALL, re.IGNORECASE),
            "mh_listen_all",
            "播放整个列表",
            run_listen_all,
            priority=4,
        ),
        Route(
            re.compile(RE_LISTEN_N, re.IGNORECASE),
            "mh_listen_n",
            "播放列表中的第 N 首",
            run_listen_n,
            priority=4,
        ),
        Route(
            re.compile(RE_SWITCH, re.IGNORECASE),
            "mh_switch",
            "换源重放当前歌曲",
            run_switch,
            priority=5,
        ),
    ]
