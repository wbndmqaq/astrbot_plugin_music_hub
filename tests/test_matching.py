"""歌名/歌手匹配与多源分组（core.matching）。"""

from astrbot_plugin_music_hub.core.matching import (
    best_match,
    group_versions,
    interleave,
    parse_source_hint,
    pick_version,
    version_names,
)


def _song(name, artist, source):
    return {"name": name, "artist": artist, "source": source}


def test_best_match_prefers_exact_name_and_artist():
    cands = [
        _song("晴天", "别人的翻唱", "kg"),
        _song("晴天", "周杰伦", "ncm"),
        _song("晴天 Live", "周杰伦", "qq"),
    ]
    assert best_match(cands, "晴天", "周杰伦")["source"] == "ncm"


def test_parse_source_hint():
    src, kw = parse_source_hint("ncm:晴天")
    assert src == "ncm" and kw == "晴天"
    src2, kw2 = parse_source_hint("晴天")
    assert src2 == "auto" and kw2 == "晴天"


def test_group_versions_merges_same_name_same_artist():
    songs = [
        _song("晴天", "周杰伦", "ncm"),
        _song("晴天", "周杰伦", "kg"),
        _song("晴天", "周杰伦", "qq"),
        _song("夜曲", "周杰伦", "ncm"),
    ]
    groups = group_versions(songs)
    assert len(groups) == 2  # 同名同歌手合组，与「同名不同歌手」区分


def test_pick_version_by_source_and_names():
    songs = [_song("晴天", "周杰伦", src) for src in ("ncm", "kg", "qq")]
    group = group_versions(songs)[0]
    assert pick_version(group, "kg")["source"] == "kg"  # group["versions"] 里按源取
    names = version_names(group)
    assert names.count("/") == 2 and "酷狗" in names  # 展示名用中文标签


def test_interleave_round_robin():
    def mk(name):
        return {"name": name, "artist": "", "source": "ncm"}

    out = interleave([[mk("a1"), mk("a2")], [mk("b1")], [mk("c1"), mk("c2"), mk("c3")]])
    assert [s["name"] for s in out[:3]] == ["a1", "b1", "c1"]  # 轮流交错
    assert [s["index"] for s in out] == [1, 2, 3, 4, 5, 6]  # 重编序号
