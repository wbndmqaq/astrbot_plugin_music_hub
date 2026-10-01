"""explore 路由汇总：编排实现按域拆至 explore_rank / explore_playlist / explore_social / explore_daily，本模块负责正则绑定与路由表。"""

from __future__ import annotations

import re

from .base import Route
from .explore_common import (
    _RE_ALBUM,
    _RE_ALBUM_COMMENT,
    _RE_ARTIST,
    _RE_ARTIST_ALBUM,
    _RE_BANNER,
    _RE_DAILY,
    _RE_DJ,
    _RE_FM,
    _RE_GUESS,
    _RE_HIGHQUALITY,
    _RE_HISTORY_DAILY,
    _RE_HOT,
    _RE_HOT_ARTISTS,
    _RE_MV_SEARCH,
    _RE_NEW,
    _RE_NEW_ALBUM,
    _RE_PLAYLIST,
    _RE_PLAYLIST_CATS,
    _RE_PLAYLIST_COMMENT,
    _RE_RANDOM,
    _RE_RANK,
    _RE_RANK_TOP,
    _RE_RECOMMEND,
    _RE_RELATED_PLAYLIST,
    _RE_SIMILAR_SINGER,
    _RE_SINGER_MV,
    _RE_SUGGEST,
    _RE_THEME,
    _RE_TOP_ARTISTS,
    _RE_TOP_CARD,
    _RE_TOP_IP,
    _RE_TOP_MV,
    _RE_YUEKU,
)
from .explore_daily import (
    run_banner,
    run_daily,
    run_dj,
    run_fm,
    run_guess,
    run_history_daily,
    run_hot_search,
    run_new_albums,
    run_random,
    run_recommend,
    run_suggest,
)
from .explore_playlist import (
    run_album,
    run_highquality,
    run_playlist,
    run_playlist_categories,
    run_related_playlists,
    run_theme,
    run_yueku,
)
from .explore_rank import (
    run_artist,
    run_hot_artists,
    run_mv_search,
    run_new_songs,
    run_rank,
    run_rank_top,
    run_top_artists,
    run_top_card,
    run_top_ip,
    run_top_mvs,
)
from .explore_social import (
    run_album_comments,
    run_artist_albums,
    run_playlist_comments,
    run_similar_singers,
    run_singer_mvs,
)


def routes() -> list[Route]:
    rs = [
        Route(re.compile(_RE_RANK, re.IGNORECASE), "mh_rank", "查看排行榜", run_rank, priority=6),
        Route(
            re.compile(_RE_RANK_TOP, re.IGNORECASE),
            "mh_rank_top",
            "编辑推荐榜单（酷狗）",
            run_rank_top,
            priority=6,
        ),
        Route(re.compile(_RE_NEW, re.IGNORECASE), "mh_new_songs", "新歌速递", run_new_songs, priority=6),
        Route(re.compile(_RE_ARTIST, re.IGNORECASE), "mh_artist", "歌手热门歌曲", run_artist, priority=6),
        Route(
            re.compile(_RE_ARTIST_ALBUM, re.IGNORECASE),
            "mh_artist_album",
            "歌手专辑",
            run_artist_albums,
            priority=7,
        ),
        Route(
            re.compile(_RE_SINGER_MV, re.IGNORECASE),
            "mh_singer_mv",
            "歌手 MV（QQ）",
            run_singer_mvs,
            priority=7,
        ),
        Route(
            re.compile(_RE_SIMILAR_SINGER, re.IGNORECASE),
            "mh_similar_singer",
            "相似歌手（QQ）",
            run_similar_singers,
            priority=7,
        ),
        Route(re.compile(_RE_ALBUM, re.IGNORECASE), "mh_album", "专辑曲目", run_album, priority=6),
        Route(re.compile(_RE_PLAYLIST, re.IGNORECASE), "mh_playlist", "歌单曲目", run_playlist, priority=6),
        Route(re.compile(_RE_HOT, re.IGNORECASE), "mh_hot", "热搜榜", run_hot_search, priority=6),
        Route(re.compile(_RE_RANDOM, re.IGNORECASE), "mh_random", "随机来一首", run_random, priority=6),
        Route(re.compile(_RE_DAILY, re.IGNORECASE), "mh_daily", "每日推荐", run_daily, priority=6),
        Route(re.compile(_RE_FM, re.IGNORECASE), "mh_fm", "私人电台", run_fm, priority=6),
        Route(
            re.compile(_RE_RECOMMEND, re.IGNORECASE), "mh_recommend", "歌单推荐", run_recommend, priority=6
        ),
        Route(
            re.compile(_RE_NEW_ALBUM, re.IGNORECASE), "mh_new_album", "新碟上架", run_new_albums, priority=6
        ),
        Route(
            re.compile(_RE_TOP_ARTISTS, re.IGNORECASE),
            "mh_top_artists",
            "歌手榜",
            run_top_artists,
            priority=6,
        ),
        Route(
            re.compile(_RE_HOT_ARTISTS, re.IGNORECASE),
            "mh_hot_artists",
            "热门歌手（网易云）",
            run_hot_artists,
            priority=6,
        ),
        Route(re.compile(_RE_MV_SEARCH, re.IGNORECASE), "mh_mv_search", "搜索 MV", run_mv_search, priority=7),
        Route(re.compile(_RE_TOP_MV, re.IGNORECASE), "mh_top_mv", "MV 榜（网易云）", run_top_mvs, priority=6),
        Route(re.compile(_RE_DJ, re.IGNORECASE), "mh_dj", "精选电台（网易云）", run_dj, priority=6),
        Route(
            re.compile(_RE_BANNER, re.IGNORECASE),
            "mh_banner",
            "首页 banner（网易云）",
            run_banner,
            priority=6,
        ),
        Route(re.compile(_RE_THEME, re.IGNORECASE), "mh_theme", "主题歌单（酷狗）", run_theme, priority=6),
        Route(
            re.compile(_RE_TOP_CARD, re.IGNORECASE),
            "mh_top_card",
            "好歌精选（酷狗）",
            run_top_card,
            priority=6,
        ),
        Route(re.compile(_RE_TOP_IP, re.IGNORECASE), "mh_top_ip", "编辑精选（酷狗）", run_top_ip, priority=6),
        Route(re.compile(_RE_YUEKU, re.IGNORECASE), "mh_yueku", "乐库推荐（酷狗）", run_yueku, priority=6),
        Route(re.compile(_RE_GUESS, re.IGNORECASE), "mh_guess", "猜你喜欢（QQ）", run_guess, priority=6),
        Route(re.compile(_RE_SUGGEST, re.IGNORECASE), "mh_suggest", "搜索建议", run_suggest, priority=6),
        Route(
            re.compile(_RE_HISTORY_DAILY, re.IGNORECASE),
            "mh_history_daily",
            "历史日推",
            run_history_daily,
            priority=6,
        ),
        Route(
            re.compile(_RE_PLAYLIST_CATS, re.IGNORECASE),
            "mh_playlist_cats",
            "歌单分类",
            run_playlist_categories,
            priority=6,
        ),
        Route(
            re.compile(_RE_HIGHQUALITY, re.IGNORECASE),
            "mh_highquality",
            "精品歌单（网易云）",
            run_highquality,
            priority=6,
        ),
        Route(
            re.compile(_RE_RELATED_PLAYLIST, re.IGNORECASE),
            "mh_related_playlist",
            "相关歌单（网易云）",
            run_related_playlists,
            priority=7,
        ),
        Route(
            re.compile(_RE_ALBUM_COMMENT, re.IGNORECASE),
            "mh_album_comment",
            "专辑评论",
            run_album_comments,
            priority=7,
        ),
        Route(
            re.compile(_RE_PLAYLIST_COMMENT, re.IGNORECASE),
            "mh_playlist_comment",
            "歌单评论",
            run_playlist_comments,
            priority=7,
        ),
    ]
    for r in rs:
        r.gated = True
    return rs
