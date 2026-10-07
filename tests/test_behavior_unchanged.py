"""「用户可见行为零变化」的护栏：把改前逐字文案固化成断言。

这些断言的取值全部来自重构前handlers 里的字面量（f-string 渲染后的最终结果）。
它们的用途不是描述"现在应该显示什么"，而是**锁住**重构前的输出——任何后续
改动若动了文案、kind 或 tip，这里立刻失败。

新增平台的正确做法：先在 core/catalog 登记 resolver，再按需扩展本文件对应 SPEC，
而不是就地改写既有 SPEC 的文案。
"""

import pytest
from astrbot_plugin_music_hub.handlers import explore_playlist as ep

# 改前 run_album / run_playlist 里三路复制粘贴的逐字文案
_ALBUM_EXPECTED = {
    "label": "专辑",
    "kind": "albums",
    "tip": "回复 听N 展开对应专辑",
    "not_found": "没有找到专辑「{kw}」",
    "empty": "专辑暂无曲目",
    "candidate_title": "专辑候选 · 叶惠美",
    "fallback_title": "叶惠美",
}
_PLAYLIST_EXPECTED = {
    "label": "歌单",
    "kind": "playlists",
    "tip": "回复 听N 展开对应歌单",
    "not_found": "没有找到歌单「{kw}」",
    "empty": "歌单暂无曲目",
    "candidate_title": "歌单候选 · 歌单名",
    "fallback_title": "歌单名",
}


@pytest.mark.parametrize(
    "spec_name,expected",
    [("_ALBUM_SPEC", _ALBUM_EXPECTED), ("_PLAYLIST_SPEC", _PLAYLIST_EXPECTED)],
)
def test_spec_matches_pre_refactor_copy(spec_name, expected):
    """SPEC 表的每个字段都必须与重构前 handlers 中的字面量逐字相同。"""
    spec = getattr(ep, spec_name)
    for key in ("label", "kind", "tip", "not_found", "empty"):
        assert spec[key] == expected[key], f"{spec_name}[{key}] 文案被改动了"


@pytest.mark.parametrize(
    "spec_name,expected",
    [("_ALBUM_SPEC", _ALBUM_EXPECTED), ("_PLAYLIST_SPEC", _PLAYLIST_EXPECTED)],
)
def test_candidate_title_renders_exactly_as_before(spec_name, expected):
    """候选列表标题改前是三次重复的 f"专辑候选 · {kw}"，这里验证渲染结果一致。"""
    spec = getattr(ep, spec_name)
    kw = "叶惠美" if spec_name == "_ALBUM_SPEC" else "歌单名"
    assert f"{spec['label']}候选 · {kw}" == expected["candidate_title"]


@pytest.mark.parametrize(
    "spec_name,expected",
    [("_ALBUM_SPEC", _ALBUM_EXPECTED), ("_PLAYLIST_SPEC", _PLAYLIST_EXPECTED)],
)
def test_not_found_copy_renders_exactly_as_before(spec_name, expected):
    kw = "叶惠美" if spec_name == "_ALBUM_SPEC" else "歌单名"
    spec = getattr(ep, spec_name)
    assert spec["not_found"].format(kw=kw) == expected["not_found"].format(kw=kw)


def test_expand_default_titles_unchanged():
    """expand_candidate 缺 meta.name 时的兜底标题，改前是「专辑」/「歌单」。"""
    from astrbot_plugin_music_hub.handlers import explore_common as ec

    assert ec._EXPAND_DEFAULT_NAME == {"album": "专辑", "playlist": "歌单"}


def test_rank_and_mv_keep_their_own_fallback_titles():
    """rank / mv 不走 catalog 表：兜底标题仍是「榜单」，MV 仍走 deliver_video 直投。"""
    import inspect

    from astrbot_plugin_music_hub.handlers import explore_common as ec

    src = inspect.getsource(ec.expand_candidate)
    assert '"榜单"' in src, "rank 的兜底标题被改动"
    assert "deliver_video" in src, "MV 展开不应经过 catalog 表"
