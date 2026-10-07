"""core/catalog.py 的行为护栏：resolver 表的语义与「文案零变化」。

这些用例刻意锁定几件容易在后续重构中被悄悄改掉的事：

1. 三平台对「没搜到」与「搜到但空」的说法不同（not_found 标志），handler 据此
   选择不同文案——若把两种空合并，run_album/run_playlist 的用户可见输出就会变。
2. QQ 的 album/songlist by-keyword 会让 ``songs`` 兼作候选列表，靠 songCount /
   trackCount 区分；这个"歧义"必须在 resolver 内消化掉。
3. 各平台 fetcher 的调用序列（含是否经 service.call 记账）是 /统计 的口径。
"""

import pytest
from astrbot_plugin_music_hub.core import SOURCE_KG, SOURCE_NCM, SOURCE_QQ, catalog


# ─────────────── 假客户端：只实现被测 resolver 会碰到的面 ───────────────
class FakeNcm:
    def __init__(self, *, cands=None, detail=None, kw_result=None):
        self._cands = cands or []
        self._detail = detail or {"album": {"name": "叶惠美"}, "songs": [{"id": "s1"}]}
        self._kw_result = kw_result
        self.calls: list[tuple] = []

    async def search(self, kw, limit=10, type_=1, page=0):
        self.calls.append(("search", kw, limit, type_))
        return self._cands

    async def album_detail(self, aid):
        self.calls.append(("album_detail", aid))
        return self._detail

    async def album_songs(self, aid, limit=30):
        self.calls.append(("album_songs", aid, limit))
        return [{"id": "s1"}]

    async def playlist_songs_by_keyword(self, kw, limit=30):
        self.calls.append(("playlist_songs_by_keyword", kw, limit))
        return self._kw_result


class FakeKg:
    def __init__(self, *, cands=None, songs=None, name="歌单名"):
        self._cands = cands or []
        self._songs = songs if songs is not None else [{"id": "s1"}]
        self._name = name
        self.calls: list[tuple] = []

    async def search(self, kw, limit=10, type_="song", page=1):
        self.calls.append(("search", kw, limit, type_))
        return self._cands

    async def album_songs(self, aid, limit=30):
        self.calls.append(("album_songs", aid, limit))
        return self._songs

    async def playlist_songs(self, pid, limit=30):
        self.calls.append(("playlist_songs", pid, limit))
        return self._songs, self._name, "cover"


class FakeQq:
    def __init__(self, *, meta=None, songs=None):
        self._meta = meta
        self._songs = songs if songs is not None else []
        self.calls: list[tuple] = []

    async def album_songs_by_keyword(self, kw, limit=30):
        self.calls.append(("album_songs_by_keyword", kw, limit))
        return self._meta, self._songs

    async def songlist_by_keyword(self, kw, limit=30):
        self.calls.append(("songlist_by_keyword", kw, limit))
        return self._meta, self._songs


# ─────────────── 专辑 resolver ───────────────
async def test_album_ncm_single_hit_expands():
    c = FakeNcm(cands=[{"id": "a1", "name": "叶惠美"}])
    res = await catalog.resolver_for("album", SOURCE_NCM)(c, "叶惠美")
    assert res.found and res.title_or("叶惠美") == "叶惠美"
    assert ("album_detail", "a1") in c.calls


async def test_album_ncm_empty_search_is_not_found():
    """搜不到 → not_found=True，handler 才会回「没有找到专辑「x」」。"""
    res = await catalog.resolver_for("album", SOURCE_NCM)(FakeNcm(cands=[]), "不存在")
    assert res.not_found is True
    assert not res.songs and not res.candidates


async def test_album_ncm_multiple_hits_become_candidates():
    cands = [{"id": "a1"}, {"id": "a2"}]
    res = await catalog.resolver_for("album", SOURCE_NCM)(FakeNcm(cands=cands), "叶惠美")
    assert res.candidates == cands and not res.songs


async def test_album_kg_uses_string_search_type():
    c = FakeKg(cands=[{"id": "a1", "name": "叶惠美"}])
    res = await catalog.resolver_for("album", SOURCE_KG)(c, "叶惠美")
    assert res.found
    # 酷狗的搜索类型是字符串 "album"（网易云是数字 10）
    assert ("search", "叶惠美", catalog.CANDIDATE_LIMIT, "album") in c.calls


async def test_album_qq_empty_is_not_not_found():
    """QQ 对「没搜到」返回 (None, [])，无法与「空专辑」区分，故 not_found 必须为 False。

    否则 run_album 会把「专辑暂无曲目」错报成「没有找到专辑「x」」。
    """
    res = await catalog.resolver_for("album", SOURCE_QQ)(FakeQq(meta=None, songs=[]), "不存在")
    assert res.not_found is False
    assert not res.songs and not res.candidates


async def test_album_qq_candidate_list_detected_by_songcount():
    """QQ 让 songs 兼作候选列表；首项带 songCount 才是候选，否则是真曲目。"""
    cands = [{"id": "a1", "songCount": 12}]
    res = await catalog.resolver_for("album", SOURCE_QQ)(FakeQq(meta=None, songs=cands), "叶惠美")
    assert res.candidates == cands


async def test_album_qq_songs_without_songcount_are_real_tracks():
    """回归：songs 非空但首项无 songCount 时必须当曲目，不能误判成候选。"""
    songs = [{"id": "s1", "name": "以父之名"}]
    res = await catalog.resolver_for("album", SOURCE_QQ)(FakeQq(meta=None, songs=songs), "以父之名")
    assert res.songs == songs and not res.candidates


# ─────────────── 歌单 resolver ───────────────
async def test_playlist_kg_single_hit_fetches_name():
    """酷狗唯一命中时要再取一次详情拿 name（列表项没有可靠歌单名）。"""
    c = FakeKg(cands=[{"id": "p1", "name": "旧名"}], name="真名")
    res = await catalog.resolver_for("playlist", SOURCE_KG)(c, "歌单")
    assert res.title_or("kw") == "真名"
    assert ("playlist_songs", "p1", catalog.DETAIL_LIMIT) in c.calls


async def test_playlist_kg_empty_search_is_not_found():
    res = await catalog.resolver_for("playlist", SOURCE_KG)(FakeKg(cands=[]), "不存在")
    assert res.not_found is True


async def test_playlist_qq_candidate_list_detected_by_trackcount():
    cands = [{"id": "p1", "trackCount": 30}]
    res = await catalog.resolver_for("playlist", SOURCE_QQ)(FakeQq(meta=None, songs=cands), "歌单")
    assert res.candidates == cands


async def test_playlist_ncm_candidates_when_meta_none():
    cands = [{"id": "p1"}]
    client = FakeNcm(kw_result=(None, cands))
    res = await catalog.resolver_for("playlist", SOURCE_NCM)(client, "歌单")
    assert res.candidates == cands


# ─────────────── 歌手 resolver ───────────────
async def test_artist_kg_empty_search_is_not_found():
    """酷狗搜不到歌手时 handler 回「没有找到歌手「x」」，与另两平台措辞不同。"""

    class C:
        async def search(self, *a, **k):
            return []

    res = await catalog.resolver_for("artist", SOURCE_KG)(C(), "不存在")
    assert res.not_found is True


# ─────────────── title_or 兜底 ───────────────
def test_title_or_falls_back_when_meta_missing():
    """QQ 可能返回 meta=None 而 songs 非空，此时不得对 None 调 .get()。"""
    assert catalog.Resolution(None, [{"id": "s"}], []).title_or("叶惠美") == "叶惠美"
    assert catalog.Resolution({}, [{"id": "s"}], []).title_or("叶惠美") == "叶惠美"
    assert catalog.Resolution("非字典", [{"id": "s"}], []).title_or("叶惠美") == "叶惠美"


# ─────────────── resolver_for 契约 ───────────────
@pytest.mark.parametrize("kind", ["album", "playlist", "artist", "album_songs", "playlist_songs"])
def test_resolver_for_covers_all_three_platforms(kind):
    """三个平台都必须有 resolver：缺一个就会在运行期回退成「该平台不支持」。"""
    for src in (SOURCE_NCM, SOURCE_KG, SOURCE_QQ):
        assert catalog.resolver_for(kind, src) is not None, f"{kind}/{src} 缺 resolver"


def test_resolver_for_returns_none_for_unknown():
    assert catalog.resolver_for("album", "spotify") is None
    assert catalog.resolver_for("不存在的kind", SOURCE_NCM) is None


def test_expand_kinds_map_covers_album_and_playlist():
    assert catalog.EXPAND_KINDS["album"] == "album_songs"
    assert catalog.EXPAND_KINDS["playlist"] == "playlist_songs"
    # 榜单与 MV 三平台方法名一致，不进表
    assert "rank" not in catalog.EXPAND_KINDS
    assert "mv" not in catalog.EXPAND_KINDS


# ─────────────── fetcher 记账口径（/统计 可见） ───────────────
async def test_random_ncm_records_both_stages():
    """网易云随机一首：私人电台与退路各自记账（改动前是两次 service.call）。"""
    recorded: list[str] = []

    async def call(src, action, coro):
        recorded.append("call")
        return await coro

    class C:
        async def personal_fm(self):
            return []

        async def personalized_newsong(self):
            return [{"id": "s1"}]

    songs = await catalog.RANDOM_FETCHERS[SOURCE_NCM](C(), call, SOURCE_NCM)
    assert songs == [{"id": "s1"}]
    assert len(recorded) == 2, "私人电台为空时应产生两条统计"


async def test_random_ncm_stops_after_first_hit():
    recorded: list[str] = []

    async def call(src, action, coro):
        recorded.append("call")
        return await coro

    class C:
        async def personal_fm(self):
            return [{"id": "s1"}]

        async def personalized_newsong(self):  # pragma: no cover - 不应被调用
            raise AssertionError("私人电台有结果时不应回退")

    songs = await catalog.RANDOM_FETCHERS[SOURCE_NCM](C(), call, SOURCE_NCM)
    assert songs == [{"id": "s1"}]
    assert len(recorded) == 1


async def test_random_qq_is_not_recorded():
    """QQ 的随机一首改动前是裸调用，不进统计——保持不变。"""
    recorded: list[str] = []

    async def call(src, action, coro):  # pragma: no cover - 不应被调用
        recorded.append("call")
        return await coro

    class C:
        async def random_song(self):
            return {"id": "s1"}

    songs = await catalog.RANDOM_FETCHERS[SOURCE_QQ](C(), call, SOURCE_QQ)
    assert songs == [{"id": "s1"}]
    assert recorded == [], "QQ 路径不应产生统计记录"


async def test_artist_albums_qq_is_not_recorded():
    recorded: list[str] = []

    async def call(src, action, coro):  # pragma: no cover - 不应被调用
        recorded.append("call")
        return await coro

    class C:
        async def artist_albums_by_keyword(self, kw, limit):
            return {"id": "s1"}, [{"id": "al1"}]

    got = await catalog.ARTIST_ALBUM_FETCHERS[SOURCE_QQ](C(), call, SOURCE_QQ, "歌手")
    assert got.found and got.albums == [{"id": "al1"}]
    assert recorded == []


async def test_artist_albums_ncm_records_two_stages():
    recorded: list[str] = []

    async def call(src, action, coro):
        recorded.append("call")
        return await coro

    class C:
        async def search(self, kw, limit, type_):
            return [{"id": "ar1"}]

        async def artist_albums(self, aid, limit):
            return [{"id": "al1"}]

    got = await catalog.ARTIST_ALBUM_FETCHERS[SOURCE_NCM](C(), call, SOURCE_NCM, "歌手")
    assert got.found and len(recorded) == 2


async def test_artist_albums_not_found_vs_empty_are_distinguished():
    """搜不到歌手 → found=False；搜到但没专辑 → found=True 且 albums 为空。

    两者对应不同文案（「没有找到歌手「x」」vs「「x」暂无专辑数据」）。
    """

    class C:
        async def search(self, *a, **k):
            return []

        async def artist_albums_by_keyword(self, kw, limit):
            return {"id": "s1"}, []

    async def call(src, action, coro):
        return await coro

    not_found = await catalog.ARTIST_ALBUM_FETCHERS[SOURCE_NCM](C(), call, SOURCE_NCM, "歌手")
    assert not_found.found is False

    empty = await catalog.ARTIST_ALBUM_FETCHERS[SOURCE_QQ](C(), call, SOURCE_QQ, "歌手")
    assert empty.found is True and empty.albums == []


# ─────────────── 能力表（平台独占能力的声明） ───────────────
def test_comment_types_exclude_qq():
    """QQ 的评论接口按歌曲 id 走，专辑/歌单评论拿不到——表里不应出现 qq。"""
    assert SOURCE_QQ not in catalog.ALBUM_COMMENT_TYPES
    assert SOURCE_QQ not in catalog.PLAYLIST_COMMENT_TYPES
    assert catalog.ALBUM_COMMENT_TYPES[SOURCE_NCM] == 10
    assert catalog.ALBUM_COMMENT_TYPES[SOURCE_KG] == "album"
    assert catalog.PLAYLIST_COMMENT_TYPES[SOURCE_NCM] == 1000
    assert catalog.PLAYLIST_COMMENT_TYPES[SOURCE_KG] == "special"


def test_playlist_category_methods_exclude_qq():
    """QQ 没有歌单分类能力，getattr 探测需先在表里落空。"""
    assert catalog.PLAYLIST_CATEGORY_METHODS == {
        SOURCE_NCM: "playlist_categories",
        SOURCE_KG: "playlist_tags",
    }


def test_recommend_category_sources_only_ncm():
    """标题里的「 · 分类」后缀只给真正按分类筛选的平台（当前仅网易云）。"""
    assert catalog.RECOMMEND_CATEGORY_SOURCES == frozenset({SOURCE_NCM})


def test_top_artist_counted_sources_preserves_stats():
    """改动前只有 ncm/kg 的歌手榜记账；QQ 是裸调用。"""
    assert catalog.TOP_ARTIST_COUNTED_SOURCES == frozenset({SOURCE_NCM, SOURCE_KG})


def test_history_daily_client_source_preserves_kg_fallback():
    """改动前「非 ncm 一律走 kg 客户端」（含 src=qq 时），如实保留。"""
    assert catalog.HISTORY_DAILY_CLIENT_SOURCE == {SOURCE_NCM: SOURCE_NCM}
    assert catalog.HISTORY_DAILY_DEFAULT_CLIENT_SOURCE == SOURCE_KG
    assert catalog.HISTORY_DAILY_HINTS[SOURCE_NCM] == "（黑胶特权）"
    assert catalog.HISTORY_DAILY_DEFAULT_HINT == "（需酷狗登录）"
