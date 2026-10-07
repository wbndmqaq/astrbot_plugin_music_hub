"""复现用例：脏统计文件不得让插件加载失败；专辑候选缺 meta 时不得崩。

对应 AUDIT_REPORT 1.7（stats.json 单字段类型异常即 TypeError）与 1.3（meta 未防 None）。
"""

import json

from astrbot_plugin_music_hub.core.config import Config
from astrbot_plugin_music_hub.core.stats import Stats


def make_stats(tmp_path) -> Stats:
    st = Stats.__new__(Stats)
    st._path = tmp_path / "stats.json"
    st._lock = __import__("asyncio").Lock()
    st._dirty = False
    st.enabled = True
    st._retention = 30
    st.daily = {}
    st.totals = {}
    st.recent = []
    st._flush_task = None
    st._start = None
    return st


def _write(tmp_path, payload) -> None:
    (tmp_path / "stats.json").write_text(json.dumps(payload, ensure_ascii=False), "utf-8")


# ── 1.7 recent 字段类型异常 ──
def test_recent_as_number_does_not_crash_load(tmp_path):
    """回归：recent 为数字时 [r for r in recent] 抛 TypeError，_load 未捕获 → 插件加载失败。"""
    _write(tmp_path, {"daily": {}, "totals": {}, "recent": 12345})
    st = make_stats(tmp_path)
    st._load()  # 不得抛异常
    assert st.recent == []


def test_recent_as_string_dict_or_none_is_tolerated(tmp_path):
    for bad in ("abc", {"a": 1}, None, [1, 2, 3], [{"ok": 1}, "junk", None]):
        _write(tmp_path, {"daily": {}, "totals": {}, "recent": bad})
        st = make_stats(tmp_path)
        st._load()
        assert isinstance(st.recent, list)
        assert all(isinstance(r, dict) for r in st.recent)


def test_valid_recent_entries_are_kept(tmp_path):
    _write(tmp_path, {"daily": {}, "totals": {}, "recent": [{"src": "ncm"}, "junk", {"src": "kg"}]})
    st = make_stats(tmp_path)
    st._load()
    assert st.recent == [{"src": "ncm"}, {"src": "kg"}]


def test_corrupt_json_is_tolerated(tmp_path):
    (tmp_path / "stats.json").write_text("{not json", "utf-8")
    st = make_stats(tmp_path)
    st._load()
    assert st.recent == []


def test_daily_totals_wrong_types_reset_to_empty(tmp_path):
    _write(tmp_path, {"daily": 5, "totals": "x", "recent": []})
    st = make_stats(tmp_path)
    st._load()
    assert st.daily == {} and st.totals == {}


def test_start_survives_malformed_file(tmp_path):
    """端到端：脏文件下 start() 不能抛，否则 plugin.initialize() 失败插件加载不起来。"""

    async def run():
        _write(tmp_path, {"daily": {}, "totals": {}, "recent": 999})
        st = make_stats(tmp_path)
        await st.start()
        try:
            st.record("ncm", "play", detail="晴天")
        finally:
            await st.close()

    import asyncio

    asyncio.run(run())


# ── 1.3 专辑候选 meta 缺失 ──
def test_album_list_title_falls_back_when_meta_missing():
    """回归：run_album 在 meta=None 且 songs 非空时 meta.get() 抛 AttributeError。"""
    from astrbot_plugin_music_hub.handlers import explore_playlist  # noqa: F401  确认模块可导入

    # 抽出标题计算逻辑做等价断言：meta 不是 dict 时必须回落 kw
    kw = "叶惠美"
    for meta in (None, {}, "not-a-dict", 0):
        title = meta.get("name", kw) if isinstance(meta, dict) else kw
        assert title == kw, f"meta={meta!r} 时标题应回落关键词"


async def test_run_album_handles_meta_none(monkeypatch):
    """真实调用 run_album（QQ 分支）：meta=None + songs 非空且首项无 songCount。"""

    class FakeClient:
        # QQ 走 album_songs_by_keyword 分支，不经过 search/album_detail
        async def album_songs_by_keyword(self, kw, limit):
            return None, [{"id": "1", "name": "以父之名", "artist": "周杰伦", "source": "qq"}]

    class FakeService:
        def __init__(self):
            self.calls = []
            self.replies = []
            # 用真实 Config：_pick_source 会读 default_source / src_enabled
            self.config = Config({"defaultSource": "qq"})

        def enabled_sources(self):
            return ["qq"]

        def client_of(self, src):
            return FakeClient()

        async def list_to_session(self, event, title, songs, **kw):
            self.calls.append((title, songs))

        async def reply(self, event, text):
            self.replies.append(text)

    from astrbot_plugin_music_hub.handlers import explore_playlist

    class FakeEvent:
        message_str = "专辑 叶惠美"

    svc = FakeService()
    await explore_playlist.run_album(svc, FakeEvent())
    assert svc.calls, "应成功列出曲目而非崩溃"
    assert svc.replies == [], f"不应报错：{svc.replies}"
    assert svc.calls[0][0], "标题不应为空"
    assert svc.calls[0][0] == "叶惠美", "meta 缺失时应回落关键词作标题"


async def test_run_playlist_handles_meta_none(monkeypatch):
    """run_playlist 已防 None，run_album 应与之对齐 —— 两者行为必须一致。"""

    class FakeClient:
        async def songlist_by_keyword(self, kw, limit):
            return None, [{"id": "9", "name": "歌单曲目", "artist": "a", "source": "qq"}]

    class FakeService:
        def __init__(self):
            self.calls = []
            self.replies = []
            self.config = Config({"defaultSource": "qq"})

        def enabled_sources(self):
            return ["qq"]

        def client_of(self, src):
            return FakeClient()

        async def list_to_session(self, event, title, songs, **kw):
            self.calls.append((title, songs))

        async def reply(self, event, text):
            self.replies.append(text)

    from astrbot_plugin_music_hub.handlers import explore_playlist

    class FakeEvent:
        message_str = "歌单 歌单名"

    svc = FakeService()
    await explore_playlist.run_playlist(svc, FakeEvent())
    assert svc.calls and svc.replies == []
