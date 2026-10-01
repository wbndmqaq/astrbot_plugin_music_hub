"""统计落盘快照（core.stats）—— 深拷贝语义（v1.0.2 回归）。"""

import asyncio

from astrbot_plugin_music_hub.core.stats import Stats


def make_stats(tmp_path) -> Stats:
    st = Stats.__new__(Stats)
    st._path = tmp_path / "stats.json"
    st._lock = asyncio.Lock()
    st._dirty = True
    st.enabled = True
    st.daily = {}
    st.totals = {}
    st.recent = []
    st._start = None
    return st


async def test_flush_snapshots_state_against_concurrent_record(tmp_path, monkeypatch):
    st = make_stats(tmp_path)
    st.record("ncm", "play", detail="晴天")
    captured = {}

    def fake_write(payload):
        captured["payload"] = payload

    monkeypatch.setattr(st, "_write_payload", fake_write)
    await st.flush()
    # 写盘完成后 record 继续累计：已捕获的快照不得跟着变（浅引用就会变）
    st.record("ncm", "play", detail="夜曲")
    day = next(iter(captured["payload"]["daily"]))
    assert captured["payload"]["daily"][day]["ncm"]["play"] == 1


async def test_flush_failure_remarks_dirty(tmp_path, monkeypatch):
    st = make_stats(tmp_path)

    def boom(_payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr(st, "_write_payload", boom)
    await st.flush()
    assert st._dirty is True  # 失败标脏，下轮重试，不静默丢统计
