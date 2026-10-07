"""点歌台队列：任务生命周期与入队边界（回归 P1-6 任务泄漏）。"""

import asyncio

import pytest
from astrbot_plugin_music_hub.core.queue import (
    _MAX_WAIT_SEC,  # noqa: F401  上限值需与实现一致
    MAX_QUEUE,
    RequestDesk,
    wait_seconds,
)

SONG = {"source": "ncm", "sid": "1", "name": "晴天", "artist": "周杰伦", "dtMs": 1000}


class _FakeStats:
    def record(self, *_a, **_kw):
        pass


class _FakeService:
    """最小 service 替身：只提供队列需要的接口。"""

    def __init__(self, umo="aiocqhttp:123"):
        self._umo = umo
        self.spawned = []
        self.stats = _FakeStats()

    def umo_of(self, _scope):
        return self._umo

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.spawned.append(task)
        return task

    async def resolve_play(self, _song):
        return {"url": "https://cdn/song.mp3"}

    async def send_audio(self, *_a, **_kw):
        return {"ok": True}

    async def send_to_scope(self, *_a, **_kw):
        pass


@pytest.fixture
def svc():
    return _FakeService()


@pytest.fixture
def qm(svc):
    return RequestDesk(svc)


async def test_player_task_registered_with_service(qm, svc):
    """播放任务必须经 service.spawn 登记，才能被 terminate 统一取消。"""
    await qm.add("g1", SONG, "u1")
    assert svc.spawned, "播放任务未登记到 service.spawn"
    assert svc.spawned[0] in list(svc.spawned)


async def test_skip_waits_for_old_player_before_starting_next(qm, svc):
    """skip 必须等旧播放任务真正退出再起新任务。

    修复前 cancel() 不等待：旧任务正在 send_audio 时会被截断，
    同时新任务已经开始发下一首 —— 两首重叠播放。
    """
    qm._queues["g1"] = {
        "items": [{"song": SONG, "user": "u1"}, {"song": {**SONG, "sid": "2"}, "user": "u2"}],
        "current": {"song": SONG, "user": "u1"},
        "task": None,
    }
    old = asyncio.create_task(asyncio.sleep(30))  # 模拟正在 sleep 的播放任务
    qm._queues["g1"]["task"] = old
    await asyncio.sleep(0)  # 让 task 真正开始运行

    nxt = await qm.skip("g1")

    assert old.done(), "skip 返回时旧播放任务仍在运行（cancel 未被等待）"
    assert nxt is not None and nxt["song"]["sid"] == "1"
    await qm.clear("g1")  # 收尾：取消新起的播放任务


async def test_wait_seconds_falls_back_when_dtms_missing():
    """缺 dtMs 时不能退化成下限 10 秒空转（否则 30 首连播变成 30×10 秒）。"""
    no_dur = {"source": "ncm", "sid": "1", "name": "x", "artist": "y"}
    # 有真实时长 → 用真实值
    assert wait_seconds({"dtMs": 240_000}, {"trial": False}) == 243
    # 试听片段 → 固定 60s
    assert wait_seconds({"dtMs": 240_000}, {"trial": True}) == 63
    # 缺 dtMs → 用估算值，不落到下限
    assert wait_seconds(no_dur, {"trial": False}) >= 180
    # 超长音频被夹到上限
    assert wait_seconds({"dtMs": 3_600_000}, {"trial": False}) == _MAX_WAIT_SEC


async def test_stop_all_cancels_and_clears(qm, svc):
    """stop_all 后不应有存活任务与残留会话（否则重载后继续推送）。"""
    await qm.add("g1", SONG, "u1")
    await qm.add("g2", SONG, "u2")
    await qm.stop_all()
    await asyncio.gather(*svc.spawned, return_exceptions=True)
    assert all(t.done() for t in svc.spawned), "仍有播放任务存活"
    assert qm._queues == {}, "stop_all 未清空队列表"


async def test_add_rejects_when_no_umo(qm):
    """会话未注册 umo 时入队应失败并回滚，不留下幽灵条目。"""
    qm._service._umo = ""
    pos = await qm.add("g1", SONG, "u1")
    assert pos == -2
    assert qm.items("g1") == []


async def test_add_respects_max_queue(qm):
    for _ in range(MAX_QUEUE):
        await qm.add("g1", SONG, "u1")
    assert await qm.add("g1", SONG, "u1") == -1


async def test_clear_keeps_scope_but_empties(qm):
    await qm.add("g1", SONG, "u1")
    qm._queues["g1"]["items"].clear()  # 避免 add 立刻消费
    assert await qm.clear("g1") >= 0
    assert qm.items("g1") == []
    assert qm.current("g1") is None
