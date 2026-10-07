"""AUDIT 2026-10-06 v3 修复的回归钉（P1/P2/P3 分配项）。

每条对应清单里一个已修复缺陷：修复退回时对应用例必须失败。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import inspect
import json
import logging
import threading
from types import SimpleNamespace

import pytest
from astrbot_plugin_music_hub.core.errors import ApiError, NotEnabledError
from astrbot_plugin_music_hub.core.queue import RequestDesk
from astrbot_plugin_music_hub.core.search import SongSearch
from astrbot_plugin_music_hub.core.service import MusicService

SONG_V3 = {"source": "ncm", "sid": "v3", "name": "晴天", "artist": "周杰伦", "dtMs": 1000}


# ──────────── 最小夹具 ────────────


def _bare_service() -> MusicService:
    """__new__ 构造最小 service：只挂当前用例需要的属性。"""
    return MusicService.__new__(MusicService)


class _FakeStatsV3:
    def record(self, *_a, **_kw):
        pass


class _FakeServiceV3:
    """队列测试用的最小 service 替身（同 test_queue._FakeService 的形态）。"""

    def __init__(self, umo="aiocqhttp:123"):
        self._umo = umo
        self.spawned = []
        self.stats = _FakeStatsV3()

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
    return _FakeServiceV3()


@pytest.fixture
def qm(svc):
    return RequestDesk(svc)


class _FakeCfg:
    max_list = 10
    default_source = "auto"

    def src_enabled(self, source):
        return True


def _client(prefix):
    class _C:
        async def search(self, keyword, limit=6, type_=None):
            return [
                {"name": f"{prefix}{i}", "artist": "歌手", "source": prefix, "sid": f"{prefix}{i}"}
                for i in range(5)
            ]

    return _C()


def _boom_client():
    class _C:
        async def search(self, keyword, limit=6, type_=None):
            raise ApiError("搜索需要登录：酷狗已禁止匿名搜索", code=152, source="kg")

    return _C()


def _search(clients) -> SongSearch:
    return SongSearch(_FakeCfg(), clients, None, lambda: ["ncm", "kg", "qq"])


# ──────────── P1-1 「听所有」分组断链 ────────────


def test_client_of_missing_source_raises_not_enabled():
    """client_of 裸下标改 get+NotEnabledError：空 source（聚合分组漏归一化）与
    未注册音源都必须给可操作的 ApiError 子类，而不是 KeyError 炸到路由兜底。"""
    svc = _bare_service()
    svc._clients = {"ncm": object()}
    assert svc.client_of("ncm") is svc._clients["ncm"]
    with pytest.raises(NotEnabledError):
        svc.client_of("")
    with pytest.raises(NotEnabledError):
        svc.client_of("kg")


async def test_play_all_plays_primary_of_group_versions():
    """auto 模式点歌存进会话的是分组 dict（无顶层 source/sid）：
    play_all 必须先归一化到 primary 再播，而不是逐首 KeyError 被吞。"""
    from astrbot_plugin_music_hub.core.matching import group_versions

    songs = [
        {"source": "ncm", "sid": "n1", "name": "晴天", "artist": "周杰伦"},
        {"source": "kg", "sid": "k1", "name": "晴天", "artist": "周杰伦"},
        {"source": "qq", "sid": "q1", "name": "七里香", "artist": "周杰伦"},
    ]
    groups = group_versions(songs)
    assert all(g.get("primary") for g in groups)

    svc = _bare_service()
    played = []

    async def fake_play_song(event, song, **kw):
        played.append(song)
        return {"ok": True}

    async def fake_reply(event, text):
        pass

    svc.play_song = fake_play_song
    svc.reply = fake_reply
    result = await svc.play_all(None, groups)
    assert [s.get("sid") for s in played] == ["n1", "q1"], "分组条目必须取 primary（默认音源优先）"
    assert result == {"ok": 2, "fail": 0}


# ──────────── P1-2 聚合截断 ────────────


async def test_versions_groups_survive_past_two():
    """三源各回 5 首不同歌、max_list=10：分组前不再整体截断，分组数必须远超 2 组
    （旧行为交错 6 条 ≈ 2 首歌 × 3 源，分组后只剩 2 组）。"""
    sm = _search({"ncm": _client("n"), "kg": _client("k"), "qq": _client("q")})
    groups, src, notices = await sm.versions("晴天")
    assert len(groups) > 2, f"分组数 {len(groups)} 说明分组前仍被截断"
    assert len(groups) <= 10
    assert src == "auto"
    assert notices == []


async def test_search_full_default_truncation_unchanged():
    """per_source 缺省时保持既有行为：聚合结果整体截断到 limit。"""
    sm = _search({"ncm": _client("n"), "kg": _client("k"), "qq": _client("q")})
    res = await sm.search_full("晴天", limit=4)
    assert len(res.songs) == 4


# ──────────── P1-3 terminate 泄漏 ────────────


async def test_terminate_survives_missing_qq_client(monkeypatch):
    """qq 库缺失时 self.qq 为 None：裸调 close() 会在 _safe 兜底之外炸出，
    后面的 HTTP 会话与渲染器清理被跳过。terminate 必须走完全程。"""
    import astrbot_plugin_music_hub.core.service as service_mod

    svc = _bare_service()
    svc.qq = None
    svc._bg_tasks = set()
    closed = []

    class _Q:
        async def stop_all(self):
            closed.append("queue")

    class _Sched:
        async def stop(self):
            closed.append("scheduler")

    class _Login:
        async def gc(self):
            closed.append("login")

    class _Sess:
        async def close(self):
            closed.append("sessions")

    class _Stats:
        async def close(self):
            closed.append("stats")

    class _Renderer:
        async def close(self):
            closed.append("renderer")

    svc.queue = _Q()
    svc.scheduler = _Sched()
    svc.login = _Login()
    svc.sessions = _Sess()
    svc.stats = _Stats()
    svc.renderer = _Renderer()

    async def fake_timers():
        closed.append("timers")

    async def fake_http():
        closed.append("http")

    monkeypatch.setattr(service_mod, "cancel_cleanup_timers", fake_timers)
    monkeypatch.setattr(service_mod, "close_session", fake_http)

    await svc.terminate()
    assert closed == ["queue", "timers", "scheduler", "login", "sessions", "stats", "http", "renderer"]


# ──────────── P2-9 remote 假成功 ────────────


async def test_send_audio_to_false_send_returns_not_ok(monkeypatch, tmp_path):
    """context.send_message 查无平台返回 False 而不抛错：语音/文件通道按发送失败
    处理，文本兜底也失败时整体记 ok=False（此前假成功，点歌台会继续空转）。"""
    from astrbot_plugin_music_hub.core import remote as remote_mod

    local = tmp_path / "a.mp3"
    local.write_bytes(b"x")

    async def fake_dl(*a, **kw):
        return {"filePath": str(local)}

    async def fake_temp():
        return tmp_path

    def fake_cleanup(path, sec):
        pass

    monkeypatch.setattr(remote_mod, "download_audio", fake_dl)
    monkeypatch.setattr(remote_mod, "get_temp_dir", fake_temp)
    monkeypatch.setattr(remote_mod, "schedule_cleanup", fake_cleanup)
    monkeypatch.setattr(
        remote_mod,
        "caps_for_name",
        lambda name: SimpleNamespace(
            vocal=False, file=True, native_card=False, passive_limited=False, notes={}
        ),
    )

    class _Ctx:
        async def send_message(self, umo, chain):
            return False  # 框架契约：查无平台

    async def fake_resolve(song):
        return {"url": "http://x/a.mp3", "quality": "128"}

    svc = SimpleNamespace(
        config=SimpleNamespace(download_timeout=30, keep_file_sec=60),
        context=_Ctx(),
        resolve_play=fake_resolve,
    )

    song = {"source": "ncm", "sid": "1", "name": "晴天", "artist": "周杰伦"}
    result = await remote_mod.send_audio_to(svc, "ghost:GroupMessage:1", song, {"url": "http://x/a.mp3"})
    assert result["ok"] is False, "send_message 返回 False 不得按成功记"
    assert result["reason"] == "send_fail"


# ──────────── P2-10 versions 丢 notices ────────────


async def test_versions_returns_login_notices():
    """聚合取数收集的「需登录」提示必须随 versions 返回（零结果时唯一可操作线索）。"""
    sm = _search({"ncm": _client("n"), "kg": _boom_client(), "qq": _client("q")})
    groups, _src, notices = await sm.versions("晴天")
    assert any("酷狗" in n and "登录" in n for n in notices), f"notices 丢失：{notices}"


# ──────────── P2-11 scheduler.enable 连带停订阅推送 ────────────


async def test_unsub_daily_appends_scheduler_hint_when_disabled():
    from astrbot_plugin_music_hub.handlers.subscribe import run_unsub_daily

    toggled = []
    replies = []

    async def set_daily(umo, scope, on):
        toggled.append(on)

    async def fake_reply(event, text):
        replies.append(text)

    svc = SimpleNamespace(
        config=SimpleNamespace(scheduler_enable=False),
        subs=SimpleNamespace(set_daily=set_daily),
        scope=lambda event: "g",
        reply=fake_reply,
    )
    await run_unsub_daily(svc, SimpleNamespace(unified_msg_origin="aiocqhttp:1"))
    assert toggled == [False]
    assert replies and "scheduler.enable" in replies[0], "定时任务关闭时必须提示推送不会执行"


async def test_unsub_daily_no_hint_when_scheduler_enabled():
    from astrbot_plugin_music_hub.handlers.subscribe import run_unsub_daily

    replies = []

    async def set_daily(umo, scope, on):
        pass

    async def fake_reply(event, text):
        replies.append(text)

    svc = SimpleNamespace(
        config=SimpleNamespace(scheduler_enable=True),
        subs=SimpleNamespace(set_daily=set_daily),
        scope=lambda event: "g",
        reply=fake_reply,
    )
    await run_unsub_daily(svc, SimpleNamespace(unified_msg_origin="aiocqhttp:1"))
    assert replies and "scheduler.enable" not in replies[0]


def test_schema_scheduler_enable_hint_documents_push_dependency():
    from pathlib import Path

    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text("utf-8"))
    hint = schema["scheduler"]["items"]["enable"].get("hint", "")
    assert "订阅推送" in hint, "enable 的 hint 应说明会连带停掉订阅推送"


# ──────────── P3a 搜索失败退冷却 ────────────


async def test_song_request_releases_cooldown_on_search_error():
    from astrbot_plugin_music_hub.handlers.play import _song_request

    svc = SimpleNamespace(
        config=SimpleNamespace(default_source="auto"),
        check_song_request=lambda: None,
        check_cooldown=lambda event: None,
        release_cooldown=lambda event: released.append(1),
        reply=None,
    )
    released = []
    replies = []

    async def fake_versions(keyword):
        raise ApiError("接口挂了", source="ncm")

    async def fake_reply(event, text):
        replies.append(text)

    svc.search_versions = fake_versions
    svc.reply = fake_reply

    await _song_request(svc, None, "晴天")
    assert released == [1], "搜索抛 ApiError 时必须退冷却"
    assert replies, "失败要有可见回复"


async def test_enqueue_releases_cooldown_on_full_queue():
    from astrbot_plugin_music_hub.handlers.queue import run_enqueue

    released = []
    replies = []

    async def fake_search(keyword, limit=3):
        return [{"source": "ncm", "sid": "1", "name": "晴天", "artist": "周杰伦"}], "ncm"

    async def fake_add(scope, song, requester):
        return -1

    async def fake_reply(event, text):
        replies.append(text)

    svc = SimpleNamespace(
        config=SimpleNamespace(cooldown_sec=30),
        check_song_request=lambda: None,
        check_cooldown=lambda event: None,
        release_cooldown=lambda event: released.append(1),
        search_songs=fake_search,
        scope=lambda event: "g",
        queue=SimpleNamespace(add=fake_add),
        reply=fake_reply,
    )
    ev = SimpleNamespace(message_str="排队 晴天", get_sender_name=lambda: "用户")
    await run_enqueue(svc, ev)
    assert released == [1], "队列已满分支必须退冷却"
    assert replies and "已满" in replies[0]


# ──────────── P3c subs：发送成功才记 seen / 只并本歌手 sid ────────────


def _subs_mgr(send_result):
    """(Subscriptions, kv, sent)：kg 固定回一首周杰伦的歌，send_to_umo 结果可控。"""
    from astrbot_plugin_music_hub.core import subs as subs_mod

    kv = {}
    sent = []

    class _Kg:
        async def followed_new_songs(self, limit):
            return [{"sid": "s1", "name": "A", "artist": "周杰伦"}]

    async def get_kv(key, default=None):
        return kv.get(key, default)

    async def put_kv(key, value):
        kv[key] = value

    async def send_to_umo(umo, text):
        sent.append(text)
        return send_result

    kg = _Kg()
    svc = SimpleNamespace(
        config=SimpleNamespace(enable=True),
        kg=kg,
        client_of=lambda src: {"kg": kg}[src],
        get_kv=get_kv,
        put_kv=put_kv,
        send_to_umo=send_to_umo,
    )
    return subs_mod.Subscriptions(svc), kv, sent


async def test_subs_seen_not_merged_when_send_fails():
    mgr, kv, sent = _subs_mgr(False)
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    artist = mgr._subs["umo1"]["artists"][0]
    await mgr.push_all()
    assert artist["seen"] == [], "发送失败这批 sid 不得记入 seen（否则下轮永不重推）"
    assert sent, "失败前已尝试发送"
    assert kv["subs_v1"]["umo1"]["artists"][0]["seen"] == [], "seen 变更不得落盘"


async def test_subs_seen_merged_only_for_matching_artist():
    mgr, _kv, _sent = _subs_mgr(True)
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    await mgr.add_artist("umo1", "g1", {"id": "10", "name": "林俊杰"})
    await mgr.push_all()
    zhou = next(a for a in mgr._subs["umo1"]["artists"] if a["name"] == "周杰伦")
    lin = next(a for a in mgr._subs["umo1"]["artists"] if a["name"] == "林俊杰")
    assert zhou["seen"] == ["s1"], "只并入命中 _same_artist 的 sid"
    assert lin["seen"] == [], "别人的新歌不得挤占本歌手的 seen 名额"


async def test_subs_daily_pushed_once_per_day():
    """回归：定时任务失败当日重试整轮，日推没有当日标记时会重复推送十几次。

    每个（有日推订阅的）会话每天只应收到一次日推——含获取失败的占位文案；
    歌手新歌另有 seen 环去重，不受影响。"""
    from astrbot_plugin_music_hub.core import subs as subs_mod

    kv: dict = {}
    sent: list[str] = []
    daily_calls = 0

    class _Ncm:
        async def daily_recommend(self):
            nonlocal daily_calls
            daily_calls += 1
            return [{"name": "晴天", "artist": "周杰伦"}]

    class _Kg:
        async def followed_new_songs(self, limit):
            return []

    async def get_kv(key, default=None):
        return kv.get(key, default)

    async def put_kv(key, value):
        kv[key] = value

    async def send_to_umo(umo, text):
        sent.append(text)
        return True

    ncm, kg = _Ncm(), _Kg()
    svc = SimpleNamespace(
        config=SimpleNamespace(enable=True),
        ncm=ncm,
        kg=kg,
        client_of=lambda src: {"ncm": ncm, "kg": kg}[src],
        get_kv=get_kv,
        put_kv=put_kv,
        send_to_umo=send_to_umo,
    )
    mgr = subs_mod.Subscriptions(svc)
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})  # 顺带建行
    await mgr.set_daily("umo1", "g1", True)

    # 定时任务失败 → 当日整轮重跑：日推只发第一次
    await mgr.push_all()
    await mgr.push_all()
    await mgr.push_all()
    assert daily_calls == 1, "日推数据只应在首轮取一次"
    daily_pushes = [t for t in sent if "日推" in t]
    assert len(daily_pushes) == 1, f"日推每天只推一次，实际推送 {len(daily_pushes)} 次"
    today = datetime.date.today().isoformat()
    assert kv["subs_v1"]["umo1"]["daily_date"] == today, "当日标记必须落盘，重载后也不重推"


async def test_subs_daily_retries_when_send_fails():
    """发送失败不落当日标记：下一轮重推，保证日推不因单次发送故障而整天丢失。"""
    from astrbot_plugin_music_hub.core import subs as subs_mod

    kv: dict = {}
    results: list[bool] = [False, True]
    sent: list[str] = []

    class _Ncm:
        async def daily_recommend(self):
            return [{"name": "晴天", "artist": "周杰伦"}]

    async def get_kv(key, default=None):
        return kv.get(key, default)

    async def put_kv(key, value):
        kv[key] = value

    async def send_to_umo(umo, text):
        sent.append(text)
        return results.pop(0)

    ncm = _Ncm()
    svc = SimpleNamespace(
        config=SimpleNamespace(enable=True),
        ncm=ncm,
        client_of=lambda src: {"ncm": ncm}[src],
        get_kv=get_kv,
        put_kv=put_kv,
        send_to_umo=send_to_umo,
    )
    mgr = subs_mod.Subscriptions(svc)
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    await mgr.set_daily("umo1", "g1", True)

    await mgr.push_all()
    await mgr.push_all()
    assert len(sent) == 2, "首轮发送失败后次轮必须重推"
    today = datetime.date.today().isoformat()
    assert kv["subs_v1"]["umo1"]["daily_date"] == today, "重推成功后补上当日标记"


# ──────────── P3d scheduler 持久化 ────────────


def _sched_service(kv):
    async def get_kv(key, default=None):
        return kv.get(key, default)

    async def put_kv(key, value):
        kv[key] = value

    return SimpleNamespace(
        config=SimpleNamespace(scheduler_enable=True, scheduler_signin_hour=23),
        get_kv=get_kv,
        put_kv=put_kv,
    )


async def test_scheduler_last_run_date_kv_roundtrip(monkeypatch):
    """_last_run_date 必须经 KV 持久化：纯内存标记重载即丢，当天会重复签到/日推。"""
    from astrbot_plugin_music_hub.core.scheduler import _KV_LAST_RUN, Scheduler

    kv: dict = {}
    svc = _sched_service(kv)
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    # 成功路径：到点且当日任务成功 → 写 KV
    s = Scheduler(svc)
    monkeypatch.setattr(s, "_target_time", lambda now: now - datetime.timedelta(seconds=1))

    async def noop():
        pass

    monkeypatch.setattr(s, "_run_daily", noop)
    await s.start()
    try:
        for _ in range(100):
            if kv.get(_KV_LAST_RUN):
                break
            await asyncio.sleep(0.01)
        assert kv.get(_KV_LAST_RUN) == today, "当日任务成功后必须落盘已跑标记"
    finally:
        await s.stop()

    # 重载后恢复：不再把同一天当成没跑过
    s2 = Scheduler(svc)
    await s2.start()
    try:
        assert s2._last_run_date == today, "启动时必须从 KV 恢复当日已跑标记"
    finally:
        await s2.stop()


async def test_scheduler_failed_day_does_not_persist(monkeypatch):
    """失败当日重试的既有语义不得破坏：_run_daily 抛错时不写 KV。"""
    from astrbot_plugin_music_hub.core.scheduler import _KV_LAST_RUN, Scheduler

    kv: dict = {}
    s = Scheduler(_sched_service(kv))
    monkeypatch.setattr(s, "_target_time", lambda now: now - datetime.timedelta(seconds=1))

    async def boom():
        raise RuntimeError("上游挂了")

    monkeypatch.setattr(s, "_run_daily", boom)
    await s.start()
    try:
        await asyncio.sleep(0.05)
        assert kv.get(_KV_LAST_RUN) is None, "失败当日不得落盘已跑标记"
        assert s._last_run_date == ""
    finally:
        await s.stop()


# ──────────── P3i / P3j 基础设施 ────────────


def test_log_buffer_read_write_from_threads_no_runtime_error():
    """emit 可来自任意线程：并发 emit + after()/latest() 迭代不得抛 RuntimeError
    （原先直接迭代 deque，WebUI 日志页偶发 500）。"""
    from astrbot_plugin_music_hub.core.logs import LogBuffer

    buf = LogBuffer()
    stop = threading.Event()
    errors = []

    def worker():
        i = 0
        while not stop.is_set():
            buf.emit(logging.LogRecord("x", logging.INFO, "p", 1, f"m{i}", None, None))
            i += 1

    def reader():
        try:
            for _ in range(300):
                buf.after(0)
                buf.latest()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads + readers:
        t.start()
    for _ in range(200):
        buf.emit(logging.LogRecord("x", logging.INFO, "p", 1, "main", None, None))
    stop.set()
    for t in threads + readers:
        t.join()
    assert not errors, f"并发读写抛错：{errors}"
    items, last = buf.latest()
    assert items and last > 0


async def test_stats_flush_loop_survives_unexpected_error(make_stats, monkeypatch):
    """_flush_loop 非预期异常不得终止循环：死掉后统计只进内存且无告警。"""
    import astrbot_plugin_music_hub.core.stats as stats_mod

    monkeypatch.setattr(stats_mod, "FLUSH_INTERVAL", 0.01)
    st = make_stats()
    calls = {"n": 0}
    orig = st.flush

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await orig()

    monkeypatch.setattr(st, "flush", flaky)
    task = asyncio.create_task(st._flush_loop())
    try:
        await asyncio.sleep(0.1)
        assert calls["n"] >= 2, "第一轮异常后循环必须继续"
        assert not task.done()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_stats_write_payload_random_tmp_survives_concurrent_writers(make_stats):
    """固定 .tmp 名会被并发孤儿写盘互踩（session.py 同类问题已修过）：随机后缀下
    8 个线程同时写盘不得产生坏文件。"""
    st = make_stats()

    def worker(i):
        try:
            st._write_payload({"daily": {}, "totals": {}, "recent": [{"i": i}] * 50})
        except OSError:
            # Windows 上并发 os.replace 同一目标可能报 PermissionError——
            # 这是替换目标层面的平台怪癖，与本修复关注的 tmp 名互踩无关；
            # 关键断言是 stats.json 始终是完整 JSON
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    data = json.loads(st._path.read_text("utf-8"))
    assert len(data["recent"]) == 50, "并发写盘后文件必须仍是完整 JSON"


# ──────────── P3o check_cooldown 同步化 ────────────


def _cooldown_event():
    return SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        get_group_id=lambda: "1",
        get_sender_id=lambda: "2",
    )


def test_check_cooldown_is_sync_and_stamps():
    assert not inspect.iscoroutinefunction(MusicService.check_cooldown), "check_cooldown 应为同步方法"
    svc = _bare_service()
    svc.config = SimpleNamespace(cooldown_sec=30)
    svc._cooldowns = {}
    ev = _cooldown_event()
    assert svc.check_cooldown(ev) is None, "首次触发应放行并盖章"
    assert svc._cooldowns, "放行时必须立即盖章"
    assert "冷却中" in svc.check_cooldown(ev), "冷却期内应给剩余提示"


def test_check_cooldown_disabled_passes():
    svc = _bare_service()
    svc.config = SimpleNamespace(cooldown_sec=0)
    svc._cooldowns = {}
    assert svc.check_cooldown(_cooldown_event()) is None
    assert svc._cooldowns == {}, "冷却关闭时不得盖章"


# ──────────── P3g queue：skip 启动失败与空 scope 回收 ────────────


async def test_skip_returns_none_when_start_fails(qm, svc, monkeypatch):
    """_start_or_none 失败时 skip 返回 None，不得对着没播出去的歌播报「接下来」。"""
    qm._queues["g1"] = {"items": [{"song": SONG_V3, "by": "u1"}], "current": None, "task": None}

    def broken_spawn(coro):
        coro.close()
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(svc, "spawn", broken_spawn)
    assert await qm.skip("g1") is None
    assert qm.items("g1"), "启动失败不得吞掉队列条目"


async def test_run_skip_reports_start_failure_not_next():
    from astrbot_plugin_music_hub.handlers.queue import run_skip

    replies = []

    class _Q:
        async def skip(self, scope):
            return None

        def items(self, scope):
            return [{"song": {"name": "晴天"}}]

    async def fake_reply(event, text):
        replies.append(text)

    svc = SimpleNamespace(scope=lambda event: "g", queue=_Q(), reply=fake_reply)
    await run_skip(svc, SimpleNamespace())
    assert replies and "启动失败" in replies[0], "启动失败不得误报「没有下一首」"


async def test_player_reclaims_scope_after_natural_end(qm, monkeypatch):
    """自然播完后回收空 scope 的 _queues/_locks 条目，不再永久驻留。"""
    import astrbot_plugin_music_hub.core.queue as queue_mod

    monkeypatch.setattr(queue_mod, "wait_seconds", lambda song, play: 0.01)
    await qm.add("g1", SONG_V3, "u1")
    for _ in range(100):
        if "g1" not in qm._queues:
            break
        await asyncio.sleep(0.02)
    assert "g1" not in qm._queues and "g1" not in qm._locks


async def test_clear_reclaims_empty_scope(qm):
    qm._queues.setdefault("g1", {"items": [], "current": None, "task": None})
    qm._locks.setdefault("g1", asyncio.Lock())
    await qm.clear("g1")
    assert "g1" not in qm._queues and "g1" not in qm._locks


# ──────────── P3h login：wait_done 超时不置终态 ────────────


async def test_wait_done_timeout_keeps_state_open():
    """超时只让等待方拿到 False：状态机留给 _drive 判定，凭证到手仍可置 done 落盘。"""
    from astrbot_plugin_music_hub.core.login import LoginFlows, LoginSession

    session = LoginSession("ncm")
    flows = LoginFlows(None)
    assert await flows.wait_done(session, timeout=0.01) is False
    assert session.state == "wait", "超时不得盖终态（否则 _drive 的 done 会被拒绝）"
    assert session.set_state("done") is True, "驱动稍后完成时状态机必须仍可置 done"


async def test_wait_done_returns_terminal_session():
    from astrbot_plugin_music_hub.core.login import LoginFlows, LoginSession

    session = LoginSession("ncm")
    flows = LoginFlows(None)
    session.set_state("done")
    out = await flows.wait_done(session, timeout=1)
    assert out is session and out.state == "done"


async def test_cancel_source_waits_for_old_task():
    """cancel_source 补 gather：旧轮询任务不落地就返回会继续打上游登录接口。"""
    import time as time_mod

    from astrbot_plugin_music_hub.core.login import LoginFlows, LoginSession

    exited = []
    session = LoginSession("ncm", extra="k")

    def spawn(coro):
        return asyncio.create_task(coro)

    flows = LoginFlows(SimpleNamespace(spawn=spawn))

    async def fake_drive():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # 模拟收尾耗时
            exited.append(time_mod.monotonic())
            raise

    session.task = spawn(fake_drive())
    flows.sessions[session.ticket] = session
    await asyncio.sleep(0.01)  # 让旧任务真正开跑（cancel 对未启动的任务体不会执行）
    await flows.cancel_source("ncm")
    assert exited, "cancel_source 必须等旧任务真正退出"
