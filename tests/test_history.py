"""播放历史与分页缓存（core.history）—— 纯内存结构。"""

from astrbot_plugin_music_hub.core.history import HISTORY_LIMIT, HistoryStore


def test_record_trims_to_limit():
    store = HistoryStore()
    for i in range(HISTORY_LIMIT + 5):
        store.record("g1", {"name": f"s{i}", "artist": "a", "source": "ncm"})
    rows = store.of("g1")
    assert len(rows) == HISTORY_LIMIT
    assert rows[-1]["name"] == f"s{HISTORY_LIMIT + 4}"


def test_record_strips_raw_but_keeps_song():
    store = HistoryStore()
    store.record("g1", {"name": "晴天", "artist": "周杰伦", "source": "ncm", "raw": {"big": "payload"}})
    row = store.of("g1")[0]
    assert "raw" not in row["song"]  # 存归一化结构，回放仍可用且不带原始响应


def test_pager_expiry():
    store = HistoryStore()
    store.set_pager("g1", "lyric", {"page": 2})
    assert store.get_pager("g1", "lyric")
    assert store.get_pager("g1", "comment") is None  # kind 不匹配


def test_all_orders_by_latest(monkeypatch):
    now = [1000]
    monkeypatch.setattr("astrbot_plugin_music_hub.core.history.time.time", lambda: now[0])
    store = HistoryStore()
    store.record("g1", {"name": "a", "artist": "", "source": "ncm"})
    now[0] = 2000
    store.record("g2", {"name": "b", "artist": "", "source": "kg"})
    rows = store.all(lambda scope: {"g1": "umo1", "g2": "umo2"}[scope])
    assert rows[0]["scope"] == "g2"  # 最近播放的会话排前
