"""并发与数据一致性：分页缓存隔离、统计脏标记、注册表活跃时间戳、订阅去重、登录落盘。

对应 AUDIT_REPORT 1.5 / 1.14 / 1.15 / 1.16 / 1.6。这些问题的共同后果是
「用户操作被静默丢弃」或「提示成功但数据没存」，因此逐条钉住行为。
"""

import asyncio

import pytest
from astrbot_plugin_music_hub.core.history import PAGER_TTL, HistoryStore
from astrbot_plugin_music_hub.core.registry import UmoRegistry
from astrbot_plugin_music_hub.core.stats import Stats


# ── 1.5 歌词与评论翻页缓存必须互不覆盖 ──
def test_lyric_and_comment_pagers_coexist():
    store = HistoryStore()
    store.set_pager("g1", "lyric", {"page": 2, "lines": ["a"]})
    store.set_pager("g1", "comment", {"page": 1, "items": ["c"]})
    assert store.get_pager("g1", "lyric") == {"page": 2, "lines": ["a"]}
    assert store.get_pager("g1", "comment") == {"page": 1, "items": ["c"]}


def test_pager_same_kind_still_overwrites():
    store = HistoryStore()
    store.set_pager("g1", "lyric", {"page": 1})
    store.set_pager("g1", "lyric", {"page": 2})
    assert store.get_pager("g1", "lyric") == {"page": 2}


def test_pager_kinds_are_isolated_across_scopes():
    store = HistoryStore()
    store.set_pager("g1", "lyric", {"page": 1})
    store.set_pager("g2", "comment", {"page": 5})
    assert store.get_pager("g1", "lyric") == {"page": 1}
    assert store.get_pager("g2", "comment") == {"page": 5}
    assert store.get_pager("g1", "comment") is None


def test_pager_expiry_still_works(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("astrbot_plugin_music_hub.core.history.time.time", lambda: now[0])
    store = HistoryStore()
    store.set_pager("g1", "lyric", {"page": 1})
    now[0] += PAGER_TTL + 1
    assert store.get_pager("g1", "lyric") is None


def test_expired_pager_only_drops_its_own_kind(monkeypatch):
    """回归护栏：过期清理不得误删同会话另一个 kind 的缓存。"""
    now = [1000.0]
    monkeypatch.setattr("astrbot_plugin_music_hub.core.history.time.time", lambda: now[0])
    store = HistoryStore()
    store.set_pager("g1", "lyric", {"page": 1})
    now[0] += 10
    store.set_pager("g1", "comment", {"page": 1})
    now[0] += PAGER_TTL + 1
    assert store.get_pager("g1", "lyric") is None
    assert store.get_pager("g1", "comment") is None  # 两者都已过期


# ── 1.14 落盘被取消时必须恢复脏标记 ──
def _stats(tmp_path):
    st = Stats.__new__(Stats)
    st._path = tmp_path / "stats.json"
    st._lock = asyncio.Lock()
    st._dirty = True
    st.enabled = True
    st._retention = 30
    st.daily = {}
    st.totals = {}
    st.recent = []
    st._flush_task = None
    st._start = None
    return st


async def test_cancelled_flush_restores_dirty_flag(tmp_path, monkeypatch):
    """回归：_dirty 先置 False 再 await，取消时脏标记丢失 → 统计永久丢失。"""
    st = _stats(tmp_path)
    entered = asyncio.Event()

    def slow_write(_payload):
        entered.set()
        # 同步阻塞直到被取消无法打断，用极短 sleep 模拟磁盘慢
        import time as _t

        _t.sleep(0.05)

    monkeypatch.setattr(st, "_write_payload", slow_write)
    task = asyncio.create_task(st.flush())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert st._dirty is True, "取消后必须恢复脏标记，否则这批统计永久丢失"


async def test_flush_not_dirty_is_noop(tmp_path, monkeypatch):
    st = _stats(tmp_path)
    st._dirty = False
    called = []
    monkeypatch.setattr(st, "_write_payload", lambda p: called.append(p))
    await st.flush()
    assert called == []


async def test_flush_failure_restores_dirty(tmp_path, monkeypatch):
    st = _stats(tmp_path)

    def boom(_p):
        raise RuntimeError("disk full")

    monkeypatch.setattr(st, "_write_payload", boom)
    await st.flush()
    assert st._dirty is True


# ── 1.15 同一 umo 的活跃时间戳必须刷新 ──
def _reg():
    saved = {}

    async def kv_get(_k, default=None):
        return saved.get("k", default)

    async def kv_put(_k, v):
        saved["k"] = v

    return UmoRegistry(kv_get, kv_put), saved


def test_note_refreshes_timestamp_for_same_umo(monkeypatch):
    """回归：same umo 提前 return 导致 ts 永不更新，活跃会话被 save() 优先裁掉。"""

    now = [1000]
    monkeypatch.setattr("astrbot_plugin_music_hub.core.registry.time.time", lambda: now[0])
    reg, _ = _reg()
    assert reg.note("g1", "aiocqhttp:GroupMessage:111") is True
    now[0] = 5000
    reg.note("g1", "aiocqhttp:GroupMessage:111")  # 同一 umo 再活跃
    assert reg.rows()[0]["ts"] == 5000, "同一 umo 的再次活跃必须刷新 ts"


def test_note_returns_false_for_unchanged_umo_without_saving(monkeypatch):

    now = [1000]
    monkeypatch.setattr("astrbot_plugin_music_hub.core.registry.time.time", lambda: now[0])
    reg, _ = _reg()
    reg.note("g1", "aiocqhttp:GroupMessage:111")
    now[0] = 2000
    assert reg.note("g1", "aiocqhttp:GroupMessage:111") is False, "内容未变不应触发落盘"


def test_umo_change_still_reports_true():
    reg, _ = _reg()
    reg.note("g1", "aiocqhttp:GroupMessage:111")
    assert reg.note("g1", "aiocqhttp:GroupMessage:222") is True


async def test_active_scope_survives_save_trimming(monkeypatch):
    """活跃会话必须留在持久化结果里，陈旧会话被裁掉。"""

    now = [1000]
    monkeypatch.setattr("astrbot_plugin_music_hub.core.registry.time.time", lambda: now[0])
    reg, saved = _reg()
    reg.note("old", "aiocqhttp:GroupMessage:1")
    now[0] = 2000
    reg.note("new", "aiocqhttp:GroupMessage:2")
    now[0] = 3000
    reg.note("new", "aiocqhttp:GroupMessage:2")  # 再次活跃
    now[0] = 4000
    reg.note("old", "aiocqhttp:GroupMessage:1")  # 陈旧会话也活跃一次但时间更早
    await reg.save()
    assert "new" in saved["k"]


# ── 1.16 订阅去重必须是并集而非覆盖 ──
class _SubsService:
    def __init__(self):
        self.kv = {}
        self.sent = []
        self.config = type("C", (), {"enable": True})()

    async def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    async def put_kv(self, key, value):
        self.kv[key] = value

    async def send_to_umo(self, umo, text):
        self.sent.append((umo, text))


def _mgr(songs_rounds):
    from astrbot_plugin_music_hub.core import subs as subs_mod

    svc = _SubsService()
    mgr = subs_mod.Subscriptions.__new__(subs_mod.Subscriptions)
    mgr._service = svc
    mgr._subs = {}
    rounds = iter(songs_rounds)

    class FakeKg:
        async def followed_new_songs(self, limit):
            return next(rounds)

    svc.kg = FakeKg()
    return mgr, svc


async def test_seen_ids_accumulate_across_pushes():
    """回归：seen 被当前窗口整体覆盖 → 重新进窗口的旧歌被当成新歌重复推送。"""
    mgr, _ = _mgr(
        [
            [{"sid": "s1", "name": "A", "artist": "周杰伦"}, {"sid": "s2", "name": "B", "artist": "周杰伦"}],
            [{"sid": "s2", "name": "B", "artist": "周杰伦"}, {"sid": "s3", "name": "C", "artist": "周杰伦"}],
        ]
    )
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    artist = mgr._subs["umo1"]["artists"][0]
    artist["seen"] = ["old1", "old2"]

    assert "新歌" in await mgr._push_artist(artist)
    assert "old1" in artist["seen"], "历史 seen 不得被清空"
    assert "s1" in artist["seen"] and "s2" in artist["seen"]

    part = await mgr._push_artist(artist)
    assert "C" in part, "第二轮应只推新歌 C（sid=s3）"
    assert "B" not in part, "已推过的 B 不得重复推送"
    assert "old1" in artist["seen"], "历史 seen 仍不得被清空"


async def test_repeated_song_is_not_pushed_twice():
    mgr, _ = _mgr(
        [
            [{"sid": "s1", "name": "A", "artist": "周杰伦"}],
            [{"sid": "s1", "name": "A", "artist": "周杰伦"}],
        ]
    )
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    artist = mgr._subs["umo1"]["artists"][0]
    assert await mgr._push_artist(artist)
    assert await mgr._push_artist(artist) == "", "同一首歌不得重复推送"


async def test_artist_matching_is_exact_not_substring():
    """退订用精确匹配，订阅也必须精确 —— 否则「周杰伦」会误推「周杰伦&费玉清」。"""
    mgr, _ = _mgr(
        [
            [
                {"sid": "x1", "name": " defects", "artist": "周杰伦&费玉清"},
                {"sid": "x2", "name": "以父之名", "artist": "周杰伦"},
            ]
        ]
    )
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    artist = mgr._subs["umo1"]["artists"][0]
    part = await mgr._push_artist(artist)
    assert "以父之名" in part
    assert "defects" not in part, "子串匹配会把「周杰伦&费玉清」的歌误判为周杰伦的新歌"


async def test_remove_artist_does_not_affect_similar_name():
    mgr, _ = _mgr([[]])
    await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"})
    await mgr.add_artist("umo1", "g1", {"id": "10", "name": "周杰伦&费玉清"})
    removed = await mgr.remove_artist("umo1", "g1", "周杰伦&费玉清")
    assert removed is not None
    assert [a["name"] for a in mgr._subs["umo1"]["artists"]] == ["周杰伦"]


async def test_duplicate_subscribe_is_rejected():
    mgr, _ = _mgr([[]])
    assert await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"}) is True
    assert await mgr.add_artist("umo1", "g1", {"id": "9", "name": "周杰伦"}) is False


async def test_daily_toggle_removes_empty_row():
    mgr, _ = _mgr([[]])
    await mgr.set_daily("umo1", "g1", True)
    assert mgr.list_of("umo1") is not None
    await mgr.set_daily("umo1", "g1", False)
    assert mgr.list_of("umo1") is None


async def test_malformed_kv_row_is_tolerated():
    """脏数据（旧 schema 缺 artists 键）不得让指令崩掉。"""
    from astrbot_plugin_music_hub.core import subs as subs_mod

    svc = _SubsService()
    svc.kv["subs_v1"] = {"umo1": {"scope": "g1"}}  # 缺 artists
    mgr = subs_mod.Subscriptions(svc)
    await mgr.load()
    await mgr.set_daily("umo1", "g1", True)
    assert mgr.list_of("umo1")["daily"] is True
    await mgr.add_artist("umo1", "g1", {"id": "1", "name": "x"})
    assert len(mgr.list_of("umo1")["artists"]) == 1


# ── 1.12 登录任务必须纳入统一生命周期 ──
class _LoginService:
    def __init__(self):
        self.spawned = []
        self._tasks = set()
        self.saved = []
        self.config = type(
            "C",
            (),
            {
                "set_src_cookie": lambda _s, src, cookie, uid="": self.saved.append((src, cookie, uid)),
                "save_async": self._save,
            },
        )()

    async def _save(self):
        return True

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.spawned.append(task)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def client_of(self, source):
        raise AssertionError("不应走到真实客户端")

    def on_login_success(self, source):
        pass


async def test_login_task_registered_with_service_spawn(monkeypatch):
    """回归：登录轮询用裸 create_task，terminate 的 cancel-all 遍历不到，
    插件卸载后仍持续请求上游登录接口。"""
    from astrbot_plugin_music_hub.core import login as login_mod

    svc = _LoginService()
    mgr = login_mod.LoginFlows(svc)

    class FakeSession:
        def __init__(self):
            from astrbot_plugin_music_hub.core.login import LoginSession

            self.s = LoginSession("ncm", "wait")

    fs = FakeSession()

    async def fake_start_ncm(self):
        return fs.s

    monkeypatch.setattr(login_mod.LoginFlows, "_start_ncm", fake_start_ncm)
    monkeypatch.setattr(
        login_mod.LoginFlows,
        "_drive",
        lambda self, s: asyncio.sleep(3600),  # 挂起以便观察登记
    )

    await mgr.start("ncm")
    assert svc.spawned, "登录任务未登记到 service.spawn，terminate 无法收尾"
    for t in svc.spawned:
        t.cancel()
    await asyncio.gather(*svc.spawned, return_exceptions=True)


# ── 1.6 凭证落盘必须能扛住「done 之后立刻取消」 ──
async def test_credential_persisted_even_if_cancelled_right_after_done(monkeypatch):
    """护栏：set_state('done') 置位 done_evt 早于 _finish 落盘，
    terminate/gc 会在此刻取消任务。_finish 位于 finally 中且能完成 await，
    因此凭证仍会落盘——此用例防止有人把 _finish 挪出 finally 或改成裸 create_task。"""
    from astrbot_plugin_music_hub.core import login as login_mod

    svc = _LoginService()
    mgr = login_mod.LoginFlows(svc)

    from astrbot_plugin_music_hub.core.login import LoginSession

    session = LoginSession("ncm", "wait")
    session.cookie = "MUSIC_U=fake"

    # _finish 在写入前让出控制权，模拟真实的 await save_async()
    original_finish = mgr._finish

    async def slow_finish(sess):
        await asyncio.sleep(0.05)
        await original_finish(sess)

    monkeypatch.setattr(mgr, "_finish", slow_finish)

    async def fake_poll(sess):
        sess.cookie = "MUSIC_U=fake"
        sess.set_state("done")  # 与真实 _drive_poll 一致：先置 done
        # 此刻 done_evt 已置位，模拟 terminate 取消任务
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_drive_poll", fake_poll)

    task = svc.spawn(mgr._drive(session))
    await session.done_evt.wait()
    # 模拟 gc()/terminate()：此时取消
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # 关键断言：尽管被取消，凭证也必须已经落盘
    assert svc.saved, "取消后凭证未落盘 —— 用户会看到「登录成功」但下次仍需重扫"
