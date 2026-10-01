"""浏览发现路由：排行榜 / 新歌 / 歌手 / 专辑 / 歌单 / 热搜 / 日推 / FM / 随机。"""

from __future__ import annotations

from ..core import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM
from ..core.errors import ApiError, NotEnabledError


def _src_of(token: str | None) -> str:
    from ..core.sources import token_to_source

    return token_to_source(token)


async def _pick_source(service, event, token: str, *, prefer: str = "") -> str:
    """确定音源：显式前缀 > 配置默认 > （prefer 优先的）可用音源；不可用时提示。"""
    src = _src_of(token)
    if src == "auto":
        src = service.config.default_source
        if src == "auto":
            enabled = service.enabled_sources()
            if not enabled:
                raise ApiError("没有可用音源：请配置酷狗/网易云 API 地址", source="")
            if prefer and prefer in enabled:
                src = prefer
            else:
                # 聚合默认音源顺序：ncm > kg > qq
                src = "ncm" if "ncm" in enabled else enabled[0]
    if not service.config.src_enabled(src):
        raise NotEnabledError(f"{SOURCE_NAMES[src]}音源未配置", source=src)
    return src


async def _locked_source(service, event, token: str, source: str, hint: str) -> str | None:
    """限定单音源的命令：返回音源 id；不满足时回复 hint 并返回 None（调用方直接 return）。"""
    src = await _pick_source(service, event, token, prefer=source)
    if src != source:
        await service.reply(event, hint)
        return None
    return src


def _to_items(rows: list[dict], kind: str, source: str = "", *, sub_key: str = "sub") -> list[dict]:
    """通用列表卡条目：从归一化行挑展示字段（run_rank / 歌手榜 / MV 榜等共用）。"""
    return [
        {
            "index": r.get("index"),
            "name": r.get("name", ""),
            "sub": r.get(sub_key, "") or "",
            "cover": r.get("cover", ""),
            "id": r.get("id", ""),
            "tag": r.get("tag", ""),
            "kind": kind,
            "source": source,
        }
        for r in rows
    ]


async def _send_generic(
    service, event, title: str, items: list[dict], src: str, tip: str = "", subtitle: str = ""
) -> None:
    """通用列表卡的统一出口（榜单/歌手/分类/MV榜等共用）。"""
    from ..core.cards import build_generic_card_data, format_generic_text

    data = build_generic_card_data(title, subtitle, items, source=src, tip=tip)
    await service.reply_card_or_text(event, data, "generic", src, format_generic_text)


async def _send_comments(service, event, target: dict, comments: dict, src: str) -> None:
    """专辑/歌单评论卡：复用 comment 模板，构造一个类歌曲的标题对象。"""
    from ..core.cards import build_comment_card_data, format_comment_text

    fake = {
        "source": src,
        "name": target.get("name", ""),
        "artist": target.get("artist", ""),
        "cover": target.get("cover", ""),
    }
    await service.reply_card_or_text(
        event,
        build_comment_card_data(fake, comments),
        "comment",
        src,
        lambda d: format_comment_text(fake, comments),
    )


def _albums_as_items(cands: list[dict], source: str = "") -> list[dict]:
    return [
        {
            "index": c.get("index"),
            "name": c.get("name", ""),
            "sub": f"{c.get('artist', '')} · {c.get('songCount', 0)} 首",
            "cover": c.get("cover", ""),
            "id": c.get("id", ""),
            "kind": "album",
            "source": source,
        }
        for c in cands
    ]


def _playlists_as_items(cands: list[dict], source: str = "") -> list[dict]:
    return [
        {
            "index": c.get("index"),
            "name": c.get("name", ""),
            "sub": f"{c.get('creator', '')} · {c.get('trackCount', 0)} 首",
            "cover": c.get("cover", ""),
            "id": c.get("id", ""),
            "kind": "playlist",
            "source": source,
        }
        for c in cands
    ]


async def _kg_new_albums(client, type_: int = 0):
    from ..core.api.http import data_of, list_of
    from ..core.api.kg import normalize_album

    params = {"pagesize": 15}
    if type_:
        params["type"] = type_
    body = await client.request("/top/album", params, anon=True)
    data = data_of(body)
    items = list_of(data.get("info") or data.get("chn"))
    return [a for a in (normalize_album(x, i) for i, x in enumerate(items)) if a]


def _qq_singer_type():
    from qqmusic_api.modules.search import SearchType

    return SearchType.SINGER


async def _expand_candidate(service, event, entry: dict, n: int) -> bool:
    """会话里的候选项展开：返回是否处理。"""
    kind = entry.get("kind", "")
    item_id = str(entry.get("id", "") or "")
    if not item_id:
        return False
    source = entry.get("source", "")
    if kind == "album":
        if source == SOURCE_NCM:
            detail = await service.ncm.album_detail(item_id)
            songs = detail.get("songs", [])
            meta = detail.get("album") or {}
        elif source == SOURCE_KG:
            songs = await service.kg.album_songs(item_id, 30)
            meta = {"name": entry.get("name", "")}
        else:
            songs = await service.qq.album_songs(item_id, 30)
            meta = {"name": entry.get("name", "")}
        if songs:
            await service.list_to_session(event, meta.get("name", "专辑"), songs, source=source)
            return True
    elif kind == "playlist":
        if source == SOURCE_NCM:
            pl, songs = await service.ncm_playlist_songs(item_id)
        elif source == SOURCE_KG:
            songs, name, cover = await service.kg.playlist_songs(item_id, 30)
            pl = {"name": name}
        else:
            meta, songs = await service.qq.songlist_songs(item_id, 30)
            pl = meta
        if songs:
            await service.list_to_session(event, pl.get("name", "歌单"), songs, source=source)
            return True
    elif kind == "rank":
        src = source
        songs = await service.client_of(src).rank_songs(item_id, 30)
        if songs:
            await service.list_to_session(event, entry.get("name", "榜单"), songs, source=src)
            return True
    elif kind == "mv":
        fake = {"source": source, "mvid": item_id, "sid": item_id, "name": entry.get("name", "")}
        r = await service.fetch_mv_url(fake)
        url = r.get("url", "")
        if not url:
            return False
        from ..core.delivery import deliver_video

        result = await deliver_video(service, event, {"name": entry.get("name", "")}, url)
        return bool(result.get("ok"))
    return False


_SRC = r"(?:(ncm|kg|qqm|qq)\s*)?"
_AREA_NEW = {"华语": 7, "欧美": 96, "日本": 8, "韩国": 16}
_QQ_AREA_NEW = {"内地": 1, "欧美": 2, "日本": 3, "韩国": 4, "最新": 5, "港台": 6}
_AREA_NCM_ALBUM = {"华语": "ZH", "欧美": "EA", "日本": "JP", "韩国": "KR"}
_QQ_AREA_ALBUM = {"内地": 1, "港台": 2, "欧美": 3, "韩国": 4, "日本": 5, "其他": 6}
_KG_AREA_ALBUM = {"华语": 1, "欧美": 2, "日本": 3, "韩国": 4}
_TIP_EXPAND = "回复 听N 展开对应条目"
_RE_RANK = rf"^\s*#?{_SRC}排行榜\s*(.*?)\s*$"
_RE_RANK_TOP = rf"^\s*#?{_SRC}排行推荐\s*$"
_RE_NEW = rf"^\s*#?{_SRC}新歌\s*(.*?)\s*$"
_RE_ARTIST = rf"^\s*#?{_SRC}歌手\s+(.+?)\s*$"
_RE_ARTIST_ALBUM = rf"^\s*#?{_SRC}歌手专辑\s+(.+?)\s*$"
_RE_SINGER_MV = rf"^\s*#?{_SRC}歌手MV\s+(.+?)\s*$"
_RE_SIMILAR_SINGER = rf"^\s*#?{_SRC}相似歌手\s+(.+?)\s*$"
_RE_ALBUM = rf"^\s*#?{_SRC}专辑\s+(.+?)\s*$"
_RE_PLAYLIST = rf"^\s*#?{_SRC}歌单\s+(.+?)\s*$"
_RE_HOT = rf"^\s*#?{_SRC}热搜\s*$"
_RE_RANDOM = rf"^\s*#?{_SRC}(?:来首歌|随机|放一首|来一首)\s*$"
_RE_DAILY = rf"^\s*#?{_SRC}(?:日推|每日推荐)\s*$"
_RE_FM = rf"^\s*#?{_SRC}FM\s*$"
_RE_RECOMMEND = rf"^\s*#?{_SRC}(?:歌单推荐|推荐歌单)\s*(.*?)\s*$"
_RE_NEW_ALBUM = rf"^\s*#?{_SRC}新碟\s*(.*?)\s*$"
_RE_TOP_ARTISTS = rf"^\s*#?{_SRC}歌手榜\s*$"
_RE_HOT_ARTISTS = rf"^\s*#?{_SRC}热门歌手\s*$"
_RE_MV_SEARCH = rf"^\s*#?{_SRC}(?:MV搜|mv搜|搜MV|搜mv)\s+(.+?)\s*$"
_RE_TOP_MV = rf"^\s*#?{_SRC}MV榜\s*$"
_RE_DJ = rf"^\s*#?{_SRC}电台\s*$"
_RE_BANNER = rf"^\s*#?{_SRC}banner\s*$"
_RE_THEME = rf"^\s*#?{_SRC}主题歌单\s*$"
_RE_TOP_CARD = rf"^\s*#?{_SRC}(?:好歌精选|热门精选)\s*$"
_RE_TOP_IP = rf"^\s*#?{_SRC}编辑精选\s*$"
_RE_YUEKU = rf"^\s*#?{_SRC}乐库\s*$"
_RE_GUESS = rf"^\s*#?{_SRC}猜你喜欢\s*$"
_RE_SUGGEST = rf"^\s*#?{_SRC}搜索建议\s+(.+?)\s*$"
_RE_HISTORY_DAILY = rf"^\s*#?{_SRC}历史日推\s*$"
_RE_PLAYLIST_CATS = rf"^\s*#?{_SRC}歌单分类\s*$"
_RE_HIGHQUALITY = rf"^\s*#?{_SRC}精品歌单\s*(.*?)\s*$"
_RE_RELATED_PLAYLIST = rf"^\s*#?{_SRC}相关歌单\s*(.*?)\s*$"
_RE_ALBUM_COMMENT = rf"^\s*#?{_SRC}专辑评论\s*(.*?)\s*$"
_RE_PLAYLIST_COMMENT = rf"^\s*#?{_SRC}歌单评论\s*(.*?)\s*$"
