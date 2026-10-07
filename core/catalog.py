"""发现类能力的适配层：把「关键词 → 专辑 / 歌单 / 歌手」的解析收敛成 resolver 表。

为什么需要这一层
----------------
handlers 层原本按 ``if src == SOURCE_NCM / elif src == SOURCE_KG / else`` 三路复制粘贴
同一段编排（``run_album`` 61 行、``run_playlist`` 71 行），而每个平台的方法名、返回结构、
「唯一命中 vs 多命中」的判定方式都不同，复制后每加一个平台就要重抄一遍。
本模块把这些差异**收敛到一处**：每个平台一个 resolver 函数，handler 只负责
「取 resolver → 调用 → 按结果分支」的编排，不再认识任何平台方法名。

依赖注入而非持有 service
------------------------
resolver 只接收 ``client``（``SourceClient`` 实例），不持有 ``MusicService``：
这样它们能被单测直接调用（不必伪造 service 的二十来个方法），也不会反向引用
service 形成循环依赖。与 ``core.search.SongSearch`` 是同一思路。

返回值用 Resolution 而不是裸 tuple
-----------------------------------
三平台「没找到」的表达并不统一（空列表 / ``meta=None`` / 两者都是），
NamedTuple 给四个槽位显式命名，调用方不必靠下标猜含义。

扩展方式
--------
新增平台两步，handlers 与本模块的调用方都不用改：

1. 在 ``core/api/`` 下实现 ``SourceClient`` 并在 registry 注册；
2. 在本文件对应的表里加一行 ``ALBUM_RESOLVERS["xx"] = _album_xx``。

平台独占能力（只有 qq 有红心、只有 ncm 有精品歌单）**不要**塞进这里——
那些是能力差异而非解析差异，留在 handlers 并配注释说明边界更清晰。
"""

from __future__ import annotations

from typing import Any, NamedTuple

from . import SOURCE_KG, SOURCE_NCM, SOURCE_QQ

# 各平台 resolver 统一的取数上限。原先是散落在 handlers 的字面量 30，
# 集中在此以免"改了一处漏了另一处"导致同一命令在不同平台取到不同数量的曲目。
# 与 core.service.LIST_SHOW_LIMIT 保持一致（值同为 30，但语义不同：那边管展示截断）。
DETAIL_LIMIT = 30
# 候选列表条数：够用户辨认，又不至于把会话刷屏
CANDIDATE_LIMIT = 6
# 歌手候选条数：歌手重名远少于专辑/歌单，取更少即可定位
ARTIST_CANDIDATE_LIMIT = 3


class Resolution(NamedTuple):
    """一次「关键词 → 目标」的解析结果。

    meta: 目标元信息（专辑/歌单/歌手）；未命中时为 None
    songs: 目标下的曲目；candidates 非空时为空
    candidates: 命中多个目标时的候选列表，交给 handler 转成选择列表
    not_found: 「搜索本身没有命中」，与「命中了但没有曲目」区分开。

    为什么要单独一个字段：三平台对空结果的说法并不一致——网易云/酷狗能分清
    「没搜到」和「搜到了但目录是空的」，而 QQ 的 album_songs_by_keyword /
    songlist_by_keyword 对两者一律返回空列表。handler 据此选择
    「没有找到专辑「x」」还是「专辑暂无曲目」。若把两种空合并成一个值，
    重构就会悄悄改掉用户看到的文案。
    """

    meta: dict | None
    songs: list[dict]
    candidates: list[dict]
    not_found: bool = False

    @property
    def found(self) -> bool:
        """是否已有曲目可展示。"""
        return bool(self.songs)

    def title_or(self, fallback: str) -> str:
        """列表标题：取 meta 里的名字，缺失/类型不符时回落到关键词。

        QQ 的 album_songs_by_keyword 在「多命中但首项无 songCount」时会返回
        meta=None 而 songs 非空（见 tests/test_regression_guarded.py），此时必须
        回落到关键词，否则会对 None 调 .get() 抛 AttributeError。
        """
        return self.meta.get("name", fallback) if isinstance(self.meta, dict) else fallback


# ─────────────────────────── 专辑 ───────────────────────────
async def _album_ncm(client: Any, kw: str) -> Resolution:
    """网易云：先按「专辑」类型搜，唯一命中再取详情（列表项字段比详情少）。

    ``type_=10`` 里的 10 是网易云搜索协议里的专辑类型号，属平台私有常量，
    不应出现在 handlers。
    """
    cands = await client.search(kw, CANDIDATE_LIMIT, type_=10)
    if not cands:
        return Resolution(None, [], [], not_found=True)
    if len(cands) > 1:
        return Resolution(None, [], cands)
    detail = await client.album_detail(cands[0]["id"])
    return Resolution(detail.get("album") or cands[0], detail.get("songs", []), [])


async def _album_kg(client: Any, kw: str) -> Resolution:
    """酷狗：搜索类型是字符串 ``album``（与网易云的裸数字、QQ 的枚举三者都不同）。"""
    cands = await client.search(kw, CANDIDATE_LIMIT, "album")
    if not cands:
        return Resolution(None, [], [], not_found=True)
    if len(cands) > 1:
        return Resolution(None, [], cands)
    meta = cands[0]
    return Resolution(meta, await client.album_songs(meta["id"], DETAIL_LIMIT), [])


async def _album_qq(client: Any, kw: str) -> Resolution:
    """QQ：``album_songs_by_keyword`` 内部已做完「唯一命中直接展开」。

    它用 ``meta is None`` 表示未命中，并让 ``songs`` 兼作候选列表，
    候选与曲目靠 ``songCount`` 字段区分。这层歧义在 resolver 内消化掉，
    handlers 只看到统一的 Resolution。
    """
    meta, songs = await client.album_songs_by_keyword(kw, DETAIL_LIMIT)
    if meta is None and songs and "songCount" in songs[0]:
        return Resolution(None, [], songs)
    return Resolution(meta, songs, [])


# ─────────────────────────── 歌单 ───────────────────────────
async def _playlist_ncm(client: Any, kw: str) -> Resolution:
    """网易云：``playlist_songs_by_keyword`` 用 ``(None, cands)`` 表达多命中。"""
    meta, songs = await client.playlist_songs_by_keyword(kw, DETAIL_LIMIT)
    if meta is None and songs:
        return Resolution(None, [], songs)
    # 网易云把「没搜到」与「搜到但空歌单」都表达成 (None, [])，故 not_found 保持 False
    return Resolution(meta, songs, [])


async def _playlist_kg(client: Any, kw: str) -> Resolution:
    """酷狗：搜索类型是 ``special``（歌单在酷狗叫「精选」）。

    唯一命中时列表项没有可靠的歌单名，必须再取一次详情拿 name。
    """
    cands = await client.search(kw, CANDIDATE_LIMIT, "special")
    if not cands:
        return Resolution(None, [], [], not_found=True)
    if len(cands) > 1:
        return Resolution(None, [], cands)
    songs, name, _cover = await client.playlist_songs(cands[0]["id"], DETAIL_LIMIT)
    return Resolution({"name": name or cands[0].get("name", "")}, songs, [])


async def _playlist_qq(client: Any, kw: str) -> Resolution:
    """QQ：与网易云同构，靠 ``trackCount`` 区分候选列表与真实曲目。"""
    meta, songs = await client.songlist_by_keyword(kw, DETAIL_LIMIT)
    if meta is None and songs and "trackCount" in songs[0]:
        return Resolution(None, [], songs)
    return Resolution(meta, songs, [])


# ─────────────────────────── 歌手 ───────────────────────────
async def _artist_ncm(client: Any, kw: str) -> Resolution:
    """网易云：``artist_songs_by_keyword`` 直接给出 (歌手, 热门歌曲)。"""
    artist, songs = await client.artist_songs_by_keyword(kw, DETAIL_LIMIT)
    return Resolution(artist, songs, [])


async def _artist_kg(client: Any, kw: str) -> Resolution:
    """酷狗：没有 by-keyword 便捷方法，搜到歌手后自己取热门歌曲。

    搜不到歌手时 not_found=True：酷狗的搜索与另两平台by-keyword 语义不同，
    handler 据此回「没有找到歌手「x」」而非「没有找到「x」的歌曲」。
    """
    cands = await client.search(kw, ARTIST_CANDIDATE_LIMIT, "author")
    if not cands:
        return Resolution(None, [], [], not_found=True)
    artist = cands[0]
    return Resolution(artist, await client.artist_songs(artist["id"], DETAIL_LIMIT), [])


async def _artist_qq(client: Any, kw: str) -> Resolution:
    """QQ：与网易云同构；``SearchType.SINGER`` 由客户端内部转换，core 不碰枚举。"""
    artist, songs = await client.artist_songs_by_keyword(kw, DETAIL_LIMIT)
    return Resolution(artist, songs, [])


# ─────────────── 按 id 展开（会话候选项「听N」） ───────────────
# 展开类 resolver 统一签名 (client, item_id, name)：部分平台只能靠条目名兜底元信息。
async def _album_songs_ncm(client: Any, item_id: str, name: str) -> tuple[dict, list[dict]]:
    """网易云：专辑详情一次返回元信息与曲目。``name`` 仅供其他平台兜底，此处不用。"""
    detail = await client.album_detail(item_id)
    return detail.get("album") or {}, detail.get("songs", [])


async def _album_songs_kg(client: Any, item_id: str, name: str) -> tuple[dict, list[dict]]:
    """酷狗只有取曲目的接口，元信息回用会话条目里的名字。"""
    return {"name": name}, await client.album_songs(item_id, DETAIL_LIMIT)


async def _album_songs_qq(client: Any, item_id: str, name: str) -> tuple[dict, list[dict]]:
    return {"name": name}, await client.album_songs(item_id, DETAIL_LIMIT)


async def _playlist_songs_ncm(client: Any, item_id: str, name: str) -> tuple[dict, list[dict]]:
    """网易云：歌单元信息与曲目是两个接口，需要都取。"""
    pl = await client.playlist_detail(item_id)
    return pl, await client.playlist_songs(item_id, DETAIL_LIMIT)


async def _playlist_songs_kg(client: Any, item_id: str, name: str) -> tuple[dict, list[dict]]:
    songs, pl_name, _cover = await client.playlist_songs(item_id, DETAIL_LIMIT)
    return {"name": pl_name}, songs


async def _playlist_songs_qq(client: Any, item_id: str, name: str) -> tuple[dict, list[dict]]:
    """QQ：``songlist_songs`` 一次返回 (元信息, 曲目)。"""
    meta, songs = await client.songlist_songs(item_id, DETAIL_LIMIT)
    return meta, songs


# ─────────────── 榜单 / 推荐 / 新碟 / 随机 ───────────────
# 下列能力三平台都有，但取数方式各不相同（方法名、是否接受分类/地区参数都不同），
# 收敛成表后 handlers 不再判断"这个平台怎么取这个数据"。


async def _top_artists_ncm(client: Any) -> list[dict]:
    return await client.toplist_artist()


async def _top_artists_kg(client: Any) -> list[dict]:
    return await client.artist_lists(0)


async def _top_artists_qq(client: Any) -> list[dict]:
    """QQ 没有歌手榜接口，用「热门」搜索歌手近似。

    搜索类型经 ``client.search_type()`` 适配点转换——core 层不 import qqmusic_api，
    枚举细节由客户端自己负责。
    """
    return await client.search("热门", 15, client.search_type("singer"))


TOP_ARTIST_FETCHERS: dict[str, Any] = {
    SOURCE_NCM: _top_artists_ncm,
    SOURCE_KG: _top_artists_kg,
    SOURCE_QQ: _top_artists_qq,
}
# 改动前只有 ncm/kg 的歌手榜取数走了 service.call（记账），QQ 那条是裸调用。
# 统计口径是 /统计 与 WebUI 可见的用户数据，重构不能顺手改，因此显式声明而非统一包一层。
TOP_ARTIST_COUNTED_SOURCES: frozenset[str] = frozenset({SOURCE_NCM, SOURCE_KG})


async def _new_songs_ncm(client: Any, area: str) -> list[dict]:
    # 地区编码表是平台私有协议的一部分，与 method 同处才有意义
    return await client.new_songs(NEW_SONG_AREAS[SOURCE_NCM].get(area, 0))


async def _new_songs_kg(client: Any, area: str) -> list[dict]:
    """酷狗 /top/song 只有「新歌速递」一个榜，不接受地区参数（area 被有意忽略）。"""
    return await client.new_songs(KG_NEW_SONG_RANK)


async def _new_songs_qq(client: Any, area: str) -> list[dict]:
    return await client.new_songs(NEW_SONG_AREAS[SOURCE_QQ].get(area, 5))


NEW_SONG_FETCHERS: dict[str, Any] = {
    SOURCE_NCM: _new_songs_ncm,
    SOURCE_KG: _new_songs_kg,
    SOURCE_QQ: _new_songs_qq,
}
# 新歌地区编码：网易云用海外榜号，QQ 用数字榜单 id
NEW_SONG_AREAS: dict[str, dict[str, int]] = {
    SOURCE_NCM: {"华语": 7, "欧美": 96, "日本": 8, "韩国": 16},
    SOURCE_QQ: {"内地": 1, "欧美": 2, "日本": 3, "韩国": 4, "最新": 5, "港台": 6},
}
# 酷狗新歌固定榜单号
KG_NEW_SONG_RANK = 21608


async def _new_albums_ncm(client: Any, area: str) -> list[dict]:
    """网易云：带地区走新碟榜，不带地区走最新上架（两个不同接口）。"""
    areas = NEW_ALBUM_AREAS[SOURCE_NCM]
    if area in areas:
        return await client.top_albums(areas[area])
    return await client.album_newest()


async def _new_albums_kg(client: Any, area: str) -> list[dict]:
    """酷狗：/top/album 的归一化在客户端里没有封装，此处按需取。

    直接用 ``client.request`` 而非给客户端加方法，是因为 core/api/* 已通过注册表
    解耦、约定不再改动；平台私有取数集中在本文件，不散回 handlers。
    """
    from .api.http import data_of, list_of
    from .api.kg import normalize_album

    params: dict[str, Any] = {"pagesize": 15}
    area_id = NEW_ALBUM_AREAS[SOURCE_KG].get(area, 0)
    if area_id:
        params["type"] = area_id
    body = await client.request("/top/album", params, anon=True)
    data = data_of(body)
    items = list_of(data.get("info") or data.get("chn"))
    return [a for a in (normalize_album(x, i) for i, x in enumerate(items)) if a]


async def _new_albums_qq(client: Any, area: str) -> list[dict]:
    return await client.new_albums(NEW_ALBUM_AREAS[SOURCE_QQ].get(area, 1), 15)


NEW_ALBUM_FETCHERS: dict[str, Any] = {
    SOURCE_NCM: _new_albums_ncm,
    SOURCE_KG: _new_albums_kg,
    SOURCE_QQ: _new_albums_qq,
}
# 新碟地区编码：网易云用两字母区码，酷狗/QQ 用数字分类 id
NEW_ALBUM_AREAS: dict[str, dict[str, Any]] = {
    SOURCE_NCM: {"华语": "ZH", "欧美": "EA", "日本": "JP", "韩国": "KR"},
    SOURCE_KG: {"华语": 1, "欧美": 2, "日本": 3, "韩国": 4},
    SOURCE_QQ: {"内地": 1, "港台": 2, "欧美": 3, "韩国": 4, "日本": 5, "其他": 6},
}


async def _recommend_playlists_ncm(client: Any, cat: str) -> list[dict]:
    """网易云是唯一支持按分类筛选推荐歌单的平台（故 handler 的标题也只对它加分类后缀）。"""
    return await client.top_playlists(cat) if cat else await client.personalized(15)


async def _recommend_playlists_kg(client: Any, cat: str) -> list[dict]:
    """酷狗推荐接口不接受分类参数，cat 被有意忽略。"""
    return await client.top_playlists()


async def _recommend_playlists_qq(client: Any, cat: str) -> list[dict]:
    return await client.recommend_playlists()


PLAYLIST_RECOMMENDERS: dict[str, Any] = {
    SOURCE_NCM: _recommend_playlists_ncm,
    SOURCE_KG: _recommend_playlists_kg,
    SOURCE_QQ: _recommend_playlists_qq,
}
# 仅这些平台会在「歌单推荐」标题里带分类后缀——与上面的参数支持范围保持一致
RECOMMEND_CATEGORY_SOURCES: frozenset[str] = frozenset({SOURCE_NCM})


async def _random_ncm(client: Any, call: Any, src: str) -> list[dict]:
    """网易云：私人电台优先，空结果退到推荐新歌。

    两段各自经 ``call`` 记账，与改动前「两次 service.call」一致——
    退路是否发生是 /统计 里可见的差异，不能合并成一次。
    """
    songs = await call(src, "explore", client.personal_fm())
    if not songs:
        songs = await call(src, "explore", client.personalized_newsong())
    return songs


async def _random_kg(client: Any, call: Any, src: str) -> list[dict]:
    """酷狗：私人电台优先，空结果退到每日推荐（同样两段各自记账）。"""
    songs = await call(src, "explore", client.personal_fm())
    if not songs:
        songs = await call(src, "explore", client.everyday_recommend())
    return songs


async def _random_qq(client: Any, call: Any, src: str) -> list[dict]:
    """QQ 没有 personal_fm，对应能力是 radar 推荐的 random_song（返回单曲）。

    此处刻意不调用 ``call``：改动前 QQ 的随机一首是裸调用，不进统计。
    保持不记账，/统计 的历史口径才不会因重构而变。
    """
    song = await client.random_song()
    return [song] if song else []


# 签名统一为 (client, call, src)：call 即 service.call，注入进来让各平台自己决定
# 哪几段需要记账，从而不在 handlers 里留下平台分支。
RANDOM_FETCHERS: dict[str, Any] = {
    SOURCE_NCM: _random_ncm,
    SOURCE_KG: _random_kg,
    SOURCE_QQ: _random_qq,
}


async def _fm_ncm(client: Any) -> Any:
    return await client.personal_fm()


async def _fm_kg(client: Any) -> Any:
    return await client.personal_fm()


async def _fm_qq(client: Any) -> Any:
    """QQ 没有 personal_fm，对应能力是 radar 推荐的 random_song（返回单曲）。"""
    return await client.random_song()


# 私人电台。返回类型不统一（QQ 返回单曲 dict、另两家返回 list），
# 由 handler 统一 isinstance 归一——与改动前一致。
FM_FETCHERS: dict[str, Any] = {
    SOURCE_NCM: _fm_ncm,
    SOURCE_KG: _fm_kg,
    SOURCE_QQ: _fm_qq,
}

# 历史日推只有网易云（黑胶特权）与酷狗（需登录）提供。改动前的分支是
# 「ncm 走 ncm，其余一律走 kg」——包括 src 为 qq 时也取酷狗客户端。
# 这个行为看起来像疏漏，但用户可见文案与实际数据源都依赖它，
# 故按原样保留并在此显式声明，而不是"顺手修正"成按平台取对应客户端。
HISTORY_DAILY_CLIENT_SOURCE: dict[str, str] = {SOURCE_NCM: SOURCE_NCM}
HISTORY_DAILY_DEFAULT_CLIENT_SOURCE = SOURCE_KG
# 各平台的失败原因提示（用户可见文案，不可合并）
HISTORY_DAILY_HINTS: dict[str, str] = {
    SOURCE_NCM: "（黑胶特权）",
    SOURCE_KG: "（需酷狗登录）",
}
HISTORY_DAILY_DEFAULT_HINT = HISTORY_DAILY_HINTS[SOURCE_KG]


class ArtistAlbums(NamedTuple):
    """歌手专辑的解析结果。

    found 与 albums 必须分开：搜不到歌手时用户看到的是「没有找到歌手「x」」，
    搜到了但没有专辑时看到的是「「x」暂无专辑数据」——两种空结果文案不同。
    """

    albums: list[dict]
    found: bool


async def _artist_albums_ncm(client: Any, call: Any, src: str, kw: str) -> ArtistAlbums:
    """网易云：搜歌手（type_=100 是歌手类型号）再取其专辑，两段各自记账。"""
    cands = await call(src, "explore", client.search(kw, 5, 100))
    if not cands:
        return ArtistAlbums([], False)
    return ArtistAlbums(await call(src, "explore", client.artist_albums(cands[0]["id"], 15)), True)


async def _artist_albums_kg(client: Any, call: Any, src: str, kw: str) -> ArtistAlbums:
    cands = await call(src, "explore", client.search(kw, ARTIST_CANDIDATE_LIMIT, "author"))
    if not cands:
        return ArtistAlbums([], False)
    return ArtistAlbums(await call(src, "explore", client.artist_albums(cands[0]["id"], 15)), True)


async def _artist_albums_qq(client: Any, call: Any, src: str, kw: str) -> ArtistAlbums:
    """QQ：客户端一次完成「搜歌手 + 取专辑」；改动前不记统计，故不调用 call。"""
    singer, albums = await client.artist_albums_by_keyword(kw, 15)
    return ArtistAlbums(albums, singer is not None)


# 签名统一为 (client, call, src, kw)：注入 call 让各平台自行决定记账段数
ARTIST_ALBUM_FETCHERS: dict[str, Any] = {
    SOURCE_NCM: _artist_albums_ncm,
    SOURCE_KG: _artist_albums_kg,
    SOURCE_QQ: _artist_albums_qq,
}


# 专辑/歌单评论的目标搜索类型。QQ 的评论接口按歌曲 id 走，拿不到专辑/歌单的评论，
# 因此只登记支持的两个平台——查表落空即"该平台不支持"。
ALBUM_COMMENT_TYPES: dict[str, Any] = {SOURCE_NCM: 10, SOURCE_KG: "album"}
PLAYLIST_COMMENT_TYPES: dict[str, Any] = {SOURCE_NCM: 1000, SOURCE_KG: "special"}

# 歌单分类：网易云叫 playlist_categories、酷狗叫 playlist_tags、QQ 无此能力。
# 存方法名而非 callable，便于 handler 用 getattr 探测支持情况。
PLAYLIST_CATEGORY_METHODS: dict[str, str] = {
    SOURCE_NCM: "playlist_categories",
    SOURCE_KG: "playlist_tags",
}

# ─────────────────────────── 统一查找 ───────────────────────────
# kind → {source: resolver}。新增 kind 在此登记，resolver_for 立即支持。
RESOLVERS: dict[str, dict[str, Any]] = {
    "album": {
        SOURCE_NCM: _album_ncm,
        SOURCE_KG: _album_kg,
        SOURCE_QQ: _album_qq,
    },
    "playlist": {
        SOURCE_NCM: _playlist_ncm,
        SOURCE_KG: _playlist_kg,
        SOURCE_QQ: _playlist_qq,
    },
    "artist": {
        SOURCE_NCM: _artist_ncm,
        SOURCE_KG: _artist_kg,
        SOURCE_QQ: _artist_qq,
    },
    "album_songs": {
        SOURCE_NCM: _album_songs_ncm,
        SOURCE_KG: _album_songs_kg,
        SOURCE_QQ: _album_songs_qq,
    },
    "playlist_songs": {
        SOURCE_NCM: _playlist_songs_ncm,
        SOURCE_KG: _playlist_songs_kg,
        SOURCE_QQ: _playlist_songs_qq,
    },
}


def resolver_for(kind: str, source: str) -> Any | None:
    """取 (kind, source) 对应的 resolver；该平台不支持此 kind 时返回 None。

    handler 拿到 None 就回退到"该平台不支持"提示——这是新增平台漏注册 resolver 时
    唯一的可见后果，不会静默走到别的平台分支上。
    """
    return RESOLVERS.get(kind, {}).get(source)


# 会话候选项 kind → 展开用的 resolver kind。榜单与 MV 不在此列：三平台方法名一致
# （rank_songs / song_mv_url），不需要按平台分发，留在 handlers 直接调即可。
EXPAND_KINDS: dict[str, str] = {
    "album": "album_songs",
    "playlist": "playlist_songs",
}


__all__ = [
    "ALBUM_COMMENT_TYPES",
    "ARTIST_ALBUM_FETCHERS",
    "CANDIDATE_LIMIT",
    "DETAIL_LIMIT",
    "EXPAND_KINDS",
    "KG_NEW_SONG_RANK",
    "NEW_ALBUM_AREAS",
    "NEW_ALBUM_FETCHERS",
    "NEW_SONG_AREAS",
    "NEW_SONG_FETCHERS",
    "PLAYLIST_CATEGORY_METHODS",
    "PLAYLIST_COMMENT_TYPES",
    "PLAYLIST_RECOMMENDERS",
    "RANDOM_FETCHERS",
    "RECOMMEND_CATEGORY_SOURCES",
    "RESOLVERS",
    "Resolution",
    "TOP_ARTIST_FETCHERS",
    "resolver_for",
]
