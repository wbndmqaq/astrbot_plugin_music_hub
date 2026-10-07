"""并发与重复调用相关的回归测试。

覆盖三类容易「看起来能跑、实际有并发问题」的地方：
- 订阅推送：N 个歌手只应请求一次「关注的歌手新歌」
- 封面取色：N 个并发请求同一封面只应下载/量化一次（缓存击穿）
- 会话注册表：并发 save 应合并，且不丢最后一次写入
"""

from __future__ import annotations

import asyncio

import pytest
from astrbot_plugin_music_hub.core import color as color_mod
from astrbot_plugin_music_hub.core.registry import UmoRegistry
from astrbot_plugin_music_hub.core.subs import Subscriptions

# ──────────── 订阅推送去重 ────────────


class _FakeKg:
    def __init__(self):
        self.calls = 0

    async def followed_new_songs(self, limit=30):
        self.calls += 1
        return [
            {"sid": "s1", "name": "新歌A", "artist": "周杰伦"},
            {"sid": "s2", "name": "新歌B", "artist": "林俊杰"},
        ]


class _FakeSubsService:
    def __init__(self):
        self.kg = _FakeKg()
        self.kv_writes = 0
        self.sent = []

    async def put_kv(self, _key, _value):
        self.kv_writes += 1

    async def send_to_umo(self, umo, text):
        self.sent.append((umo, text))


class _FakeNcm:
    async def daily_recommend(self):
        return []


@pytest.fixture
def subs_service():
    svc = _FakeSubsService()
    svc.ncm = _FakeNcm()
    return svc


async def test_subs_fetches_followed_songs_once_for_multiple_artists(subs_service):
    """订阅 3 个歌手只应请求一次 followed_new_songs。

    该接口返回的是「我关注的全部歌手的新歌」，对每个歌手各请求一次
    是 N 倍冗余，会线性推高酷狗 20028 风控概率。
    """
    sub = Subscriptions(subs_service)
    sub._subs["aiocqhttp:1"] = {
        "daily": False,
        "artists": [{"name": "周杰伦"}, {"name": "林俊杰"}, {"name": "薛之谦"}],
    }
    await sub._push_one("aiocqhttp:1", sub._subs["aiocqhttp:1"])
    assert subs_service.kg.calls == 1, f"重复请求了 {subs_service.kg.calls} 次"


async def test_subs_writes_kv_once_per_push(subs_service):
    """多个歌手的 seen 变更应合并成一次订阅表落盘。"""
    sub = Subscriptions(subs_service)
    sub._subs["aiocqhttp:1"] = {
        "daily": False,
        "artists": [{"name": "周杰伦"}, {"name": "林俊杰"}],
    }
    await sub._push_one("aiocqhttp:1", sub._subs["aiocqhttp:1"])
    assert subs_service.kv_writes == 1, f"落盘了 {subs_service.kv_writes} 次"


async def test_subs_survives_fetch_failure(subs_service):
    """取歌失败时安静跳过，不抛异常打断整轮推送。"""
    subs_service.kg.calls = -1  # 触发异常路径

    async def boom(limit=30):
        raise RuntimeError("上游 502")

    subs_service.kg.followed_new_songs = boom
    sub = Subscriptions(subs_service)
    sub._subs["aiocqhttp:1"] = {"daily": False, "artists": [{"name": "周杰伦"}]}
    await sub._push_one("aiocqhttp:1", sub._subs["aiocqhttp:1"])  # 不应抛
    assert subs_service.sent == []  # 无内容可推就不发消息


# ──────────── 封面取色缓存击穿 ────────────


@pytest.fixture(autouse=True)
def _clear_color_cache():
    color_mod._CACHE.clear()
    color_mod._INFLIGHT.clear()
    yield
    color_mod._CACHE.clear()
    color_mod._INFLIGHT.clear()


async def test_palette_dedupes_concurrent_same_url(monkeypatch):
    """10 个并发请求同一封面只应触发一次 _compute（否则 10 次下载 + 10 次量化）。"""
    calls = 0

    async def fake_compute(_url, _session):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)  # 模拟网络/量化耗时，放大并发窗口
        return {"main": "#123456"}

    monkeypatch.setattr(color_mod, "_compute", fake_compute)
    results = await asyncio.gather(*(color_mod.palette_for("https://x/cover.jpg", None) for _ in range(10)))
    assert calls == 1, f"_compute 被调用 {calls} 次（缓存击穿）"
    assert all(r == {"main": "#123456"} for r in results)


async def test_palette_caches_after_compute(monkeypatch):
    """第二次取同一封面应命中缓存，不再触发 _compute。"""
    calls = 0

    async def fake_compute(_url, _session):
        nonlocal calls
        calls += 1
        return {"main": "#abcdef"}

    monkeypatch.setattr(color_mod, "_compute", fake_compute)
    await color_mod.palette_for("https://x/c.jpg", None)
    await color_mod.palette_for("https://x/c.jpg", None)
    assert calls == 1


async def test_palette_failed_url_not_stuck_in_inflight(monkeypatch):
    """失败的 URL 不能永久留在 in-flight 里，否则后续请求永远拿到同一个坏 Future。"""

    async def boom(_url, _session):
        raise RuntimeError("download failed")

    monkeypatch.setattr(color_mod, "_compute", boom)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await color_mod.palette_for("https://x/bad.jpg", None)
    assert "https://x/bad.jpg" not in color_mod._INFLIGHT


async def test_palette_rejects_non_http():
    assert await color_mod.palette_for("", None) is None
    assert await color_mod.palette_for("javascript:alert(1)", None) is None


# ──────────── 会话注册表并发落盘 ────────────


class _SlowKv:
    def __init__(self):
        self.writes = []

    async def get(self, _key, _default=None):
        return None

    async def put(self, _key, value):
        await asyncio.sleep(0.01)  # 放大并发窗口
        self.writes.append(dict(value))


async def test_registry_concurrent_saves_converge():
    """并发 note + save 后，落盘内容必须包含最后登记的全部会话。"""
    kv = _SlowKv()
    reg = UmoRegistry(kv.get, kv.put)
    for i in range(5):
        if reg.note(f"g{i}", f"aiocqhttp:{i}"):
            asyncio.create_task(reg.save())
    await asyncio.gather(*[reg.save() for _ in range(3)])
    await asyncio.sleep(0.05)
    assert kv.writes, "没有任何落盘"
    last = kv.writes[-1]
    for i in range(5):
        assert f"g{i}" in last, f"并发落盘丢了 g{i}"


async def test_registry_save_serializes_and_flushes_dirty():
    """保存期间的 note 必须被尾随的写覆盖，不能永久丢失。"""
    kv = _SlowKv()
    reg = UmoRegistry(kv.get, kv.put)
    reg.note("g0", "aiocqhttp:0")
    task = asyncio.create_task(reg.save())
    await asyncio.sleep(0.005)  # 确保已进入 await kv.put
    reg.note("g1", "aiocqhttp:1")  # 保存进行中登记新会话
    await task
    await asyncio.sleep(0.05)
    assert "g1" in kv.writes[-1], "保存期间的登记被丢弃"
