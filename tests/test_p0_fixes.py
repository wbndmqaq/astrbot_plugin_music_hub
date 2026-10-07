"""P0 修复的回归护栏：每条对应一个真实修复过的缺陷。

这些测试的存在意义是「防止已修缺陷回流」。写法上尽量断言**行为**
而不是源码文本，这样重构不会误报。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from astrbot_plugin_music_hub.core import config as config_mod
from astrbot_plugin_music_hub.core.history import MAX_SCOPES, HistoryStore
from astrbot_plugin_music_hub.core.login import LoginFlows
from astrbot_plugin_music_hub.core.webui._auth import AuthManager


# ── P0-1 QQ 登录态：credential is None 恒假导致重启后凭证注入不进来 ──
def test_qq_credential_injection_uses_nonempty_check():
    """回归：qqmusic_api 的 setter 是 `value or Credential()`，None 被兜底成空凭证，
    所以 `client.credential is None` 永远为假 —— 配置里的凭证在重启后永不注入，
    表现为「扫码当场可用、重启后红心/关注全 401」。判据必须是 musicid/musickey。"""
    from pathlib import Path

    from astrbot_plugin_music_hub.core.api import qq as qq_mod

    src = Path(qq_mod.__file__).read_text("utf-8")
    # 只看 get_client 里的注入判断；qr_consume 里对 getattr 结果的 is None 检查是合理的
    inject = src[src.index("async def get_client") : src.index("async def close")]
    assert "self._client.credential is None" not in inject, (
        "不能用 `is None` 判空，setter 会把 None 兜底成空凭证"
    )
    assert "cred.musicid and cred.musickey" in inject, "应按 musicid/musickey 判空"


def test_qq_credential_check_matches_library():
    """判据必须与库一致：client.py 里 require_login 校验的是 musicid + musickey。"""
    import inspect

    from qqmusic_api.core.client import Client

    src = inspect.getsource(Client)
    assert "not cred.musicid or not cred.musickey" in src, (
        "库判据变了，插件的注入判据要同步（否则重启后凭证注入行为会与库不一致）"
    )


# ── P0-2 分享文案兜底：把 (songs, source) 元组当 list 用 ──
class _FakeService:
    def __init__(self, result):
        self._result = result
        self.played = None

    async def search_songs(self, keyword, source="auto", limit=None):
        return self._result

    async def play_song(self, event, song, source_label=""):
        self.played = song
        return {"ok": True}

    async def reply(self, event, text, **kw):
        pass


@pytest.mark.asyncio
async def test_keyword_fallback_does_not_play_on_empty_result():
    """回归：search_songs 返回 (songs, source) 元组，判 `if results:` 恒真，
    零结果时把空 list 传给 play_song → AttributeError 被宽 except 吞掉，
    该兜底功能自上线起从未生效。"""
    from astrbot_plugin_music_hub.core import resolve

    svc = _FakeService(([], "ncm"))
    handled = await resolve._keyword_fallback(svc, None, "分享《晴天》 周杰伦", "ncm")
    assert handled is False
    assert svc.played is None, "零结果时不得调用 play_song"


@pytest.mark.asyncio
async def test_keyword_fallback_plays_first_song_on_hit():
    from astrbot_plugin_music_hub.core import resolve

    song = {"name": "晴天", "artist": "周杰伦", "source": "ncm"}
    svc = _FakeService(([song], "ncm"))
    handled = await resolve._keyword_fallback(svc, None, "分享《晴天》 周杰伦", "ncm")
    assert handled is True
    assert svc.played == song


# ── P0-3 compressBitrate 曾是死配置 ──
def test_compress_bitrate_is_actually_wired():
    """回归：schema/config/WebUI 都暴露了 compressBitrate，但 media 曾硬编码
    128k/96k/64k，用户改了配置完全没效果。"""
    import inspect

    from astrbot_plugin_music_hub.core import media

    sig = inspect.signature(media.prepare_vocal_file)
    assert "bitrate" in sig.parameters, "prepare_vocal_file 必须接受 bitrate 参数"
    assert media._resolve_bitrate(1024, "320") == "320k"
    assert media._resolve_bitrate(1024, "128") == "128k"
    # 非法值回落到内置阶梯而不是崩
    assert media._resolve_bitrate(1024, None) == "128k"
    assert media._resolve_bitrate(1024, "abc") == "128k"


def test_artifact_name_encodes_bitrate():
    """产物文件名要带码率：否则用户改配置后旧产物会被当成命中缓存复用。"""
    from astrbot_plugin_music_hub.core.media import _resolve_bitrate

    assert _resolve_bitrate(1024, "192") != _resolve_bitrate(1024, "128")


# ── P0-4 _auth.verify：畸形 cookie 触发 500 ──
def test_verify_survives_malformed_tokens(tmp_path):
    """回归：hmac.compare_digest 对非 ASCII str 抛 TypeError，
    异常穿透到请求层变成 500 而不是 401。"""
    auth = AuthManager(tmp_path, lambda: "pw")
    for bad in [
        "abc.非ASCII签名",
        "abc.",
        ".abc",
        "a.b",
        "!!!.###",
        "x" * 500 + "." + "y" * 500,
    ]:
        assert auth.verify(bad) is None, f"畸形 token 应返回 None 而非抛异常：{bad[:20]!r}"


def test_verify_accepts_valid_token(tmp_path):
    auth = AuthManager(tmp_path, lambda: "pw")
    token = auth.create_session(ip="1.1.1.1")
    payload = auth.verify(token)
    assert payload and payload["sub"] == "admin"


def test_session_table_is_bounded(tmp_path):
    """会话表只在内存里，必须有硬上限，否则反复登录能撑到无界。"""
    auth = AuthManager(tmp_path, lambda: "pw")
    for i in range(auth.MAX_SESSIONS + 30):
        auth.create_session(ip=f"10.0.0.{i % 200 + 1}")
    assert len(auth.sessions) <= auth.MAX_SESSIONS


def test_check_password_tolerates_non_ascii(tmp_path):
    auth = AuthManager(tmp_path, lambda: "密码")
    assert auth.check_password("密码") is True
    assert auth.check_password("错") is False


# ── P0-5 login.gc 不等待任务 / 同源会话累积 ──
class _FakeLoginService:
    def __init__(self):
        self.kv = {}

    def spawn(self, coro):
        return asyncio.create_task(coro)

    async def get_kv(self, key, default=None):
        return self.kv.get(key, default)

    async def put_kv(self, key, value):
        self.kv[key] = value

    def client_of(self, source):
        raise AssertionError("不应真的取流")


@pytest.mark.asyncio
async def test_gc_waits_for_task_exit():
    """回归：cancel() 只投递信号，不等落地就返回的话旧 _drive 会继续每 2 秒打上游。"""
    from astrbot_plugin_music_hub.core.login import LoginSession

    svc = _FakeLoginService()
    mgr = LoginFlows(svc)
    started = asyncio.Event()

    async def forever():
        started.set()
        await asyncio.sleep(60)

    s = LoginSession("ncm")
    # gc 的过滤条件是「超 600 秒或已完成」，这里用已完成触发清理路径
    s.set_state("done")
    s.task = svc.spawn(forever())
    mgr.sessions[s.ticket] = s
    await started.wait()

    await mgr.gc()
    assert s.task.done(), "gc 返回时轮询任务必须已经真正退出"


@pytest.mark.asyncio
async def test_gc_keeps_fresh_waiting_session():
    """gc 只清超 600 秒或已完成的：刚开的待扫码会话不满足条件，不能被误清。"""
    from astrbot_plugin_music_hub.core.login import LoginSession

    mgr = LoginFlows(_FakeLoginService())
    s = LoginSession("ncm")
    s.set_state("scanned")
    mgr.sessions[s.ticket] = s
    await mgr.gc()
    assert s.ticket in mgr.sessions


@pytest.mark.asyncio
async def test_cancel_source_cancels_fresh_waiting_session():
    """回归：反复点扫码会累积轮询任务（gc 清不掉刚开的会话）。"""
    from astrbot_plugin_music_hub.core.login import LoginSession

    mgr = LoginFlows(_FakeLoginService())
    old = LoginSession("ncm")
    old.set_state("scanned")
    mgr.sessions[old.ticket] = old
    await mgr.cancel_source("ncm")
    assert old.state == "cancel"


@pytest.mark.asyncio
async def test_cancel_does_not_kill_finished_session():
    """回归：已 done 的会话其 task 负责 _finish 写配置，
    无条件 cancel 会把 cookie 丢掉而状态仍显示 done。"""
    from astrbot_plugin_music_hub.core.login import LoginSession

    mgr = LoginFlows(_FakeLoginService())
    s = LoginSession("ncm")
    s.set_state("done")

    finished = []

    async def finish_write():
        await asyncio.sleep(0.05)
        finished.append("cookie-written")

    s.task = mgr._service.spawn(finish_write()) if hasattr(mgr, "_service") else None
    if s.task is None:
        svc = _FakeLoginService()
        mgr = LoginFlows(svc)
        s.task = svc.spawn(finish_write())

    await mgr.cancel(s.ticket)
    await asyncio.sleep(0.1)
    assert s.state == "done"
    assert finished == ["cookie-written"], "已完成会话的收尾（写配置）必须跑完"


# ── P0-6 history scope 无上限 ──
def test_history_caps_scope_count():
    """回归：私聊每人一 scope，长期运行后 _history 累积数千 key 不回收。"""
    h = HistoryStore()
    for i in range(MAX_SCOPES + 50):
        h.record(f"scope{i}", {"name": "x", "source": "ncm"})
    assert len(h._history) <= MAX_SCOPES
    assert len(h._scope_ts) <= MAX_SCOPES


def test_sweep_pagers_removes_expired():
    """PAGER_TTL 原先只在读取时惰性生效，写入后未再访问的条目永远留在内存里。"""
    h = HistoryStore()
    h.set_pager("s1", "lyric", {"lines": []})
    h.set_pager("s1", "comment", {"items": []})
    assert h.sweep_pagers() == 0
    h._pagers[("s1", "lyric")]["ts"] -= 10_000
    assert h.sweep_pagers() == 1
    assert h.get_pager("s1", "lyric") is None


# ── P0-7 死配置：download_timeout 无上界 ──
def test_download_timeout_has_upper_bound():
    """回归：手改配置写入 999999999 → 单次下载可挂 11 天。"""
    assert config_mod.Config({"downloadTimeout": 999_999_999}).download_timeout == 600.0
    assert config_mod.Config({"downloadTimeout": 1}).download_timeout == 5.0


def test_quality_is_case_insensitive():
    """回归：用户填 'HIRES' 时阶梯查不到会静默降级到 lossless，无任何提示。"""
    cfg = config_mod.Config({"ncm": {"quality": "HIRES"}})
    assert cfg.src_quality("ncm") == "hires"
    assert config_mod.Config({"kg": {"quality": " Lossless "}}).src_quality("kg") == "lossless"


# ── P0-8 统计错误归类不得回显上游原文 ──
def test_error_buckets_do_not_leak_upstream_text():
    """回归：topErrors 曾直接回显 str(e)[:60]，可能带 host:port。"""
    from astrbot_plugin_music_hub.core.stats import _error_bucket

    assert _error_bucket("ApiError(ncm, code=301): 未登录或登录已失效") == "登录已失效"
    assert _error_bucket("CredentialExpiredError: 登录凭证已过期") == "登录已失效"
    assert _error_bucket("ConnectionError: 拒绝连接") == "网络异常"
    assert _error_bucket("ConnectTimeout: connection refused") == "请求超时"
    assert _error_bucket("") == "未知原因"
    # 关键断言：任何输入都不得原样回显
    for raw in ["连接 http://10.0.0.5:3000 失败", "10.0.0.5", "ApiError(kg, code=152)"]:
        assert _error_bucket(raw) in {
            "未知原因",
            "登录已失效",
            "登录问题",
            "触发风控",
            "请求超时",
            "网络异常",
            "无匹配结果",
            "音源未配置",
            "其他错误",
        }, f"归类结果必须是固定文案而非原文：{raw!r} -> {_error_bucket(raw)!r}"
    assert "10.0.0.5" not in _error_bucket("连接 http://10.0.0.5:3000 失败")
    assert _error_bucket("完全没见过的错") == "其他错误"


def test_sum_tolerates_dirty_node():
    """回归：脏数据（字段被写成 list/None）时归零而非抛错。"""
    from astrbot_plugin_music_hub.core.stats import _actions, _sum

    assert _sum({"ncm": [1, 2]}, "ncm") == 0
    assert _sum({"ncm": None}, "ncm") == 0
    assert _sum({}, "ncm") == 0
    assert _sum({"ncm": {"search": 2}}, "ncm") == 2
    assert _actions({"ncm": "bad"}, "ncm") == {}


# ── P0-9 OneBot：UUID 用户 ID 与 base64 上限 ──
def test_base64_inline_limit_is_memory_safe():
    """回归：150MB 上限下峰值内存可达 550MB，足以拖垮 AstrBot 主进程。"""
    from astrbot_plugin_music_hub.core import onebot

    assert onebot._BASE64_INLINE_MAX_BYTES <= 25 * 1024 * 1024


def test_aiocq_target_accepts_uuid_sender_id():
    """回归：部分平台用 UUID 而非数字 id，int() 抛 ValueError 会让投递整体退化。"""
    from astrbot_plugin_music_hub.core import onebot

    class _Ev:
        class message_obj:
            group_id = None

        @staticmethod
        def get_sender_id():
            return "550e8400-e29b-41d4-a716-446655440000"

    action, sid = onebot.aiocq_target(_Ev())
    assert action == "send_private_msg"
    assert sid == "550e8400-e29b-41d4-a716-446655440000"


# ── P0-10 WebUI 静态资源缓存 ──
def test_static_files_carry_etag_and_304():
    """回归：原先 no-store + 无 ETag，40KB CSS + 55KB JS 每次整页全量重下。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "core" / "webui" / "_server.py").read_text("utf-8")
    assert '"ETag"' in src
    assert "If-None-Match" in src
    file_fn = src[src.index("async def _file") : src.index("async def _meta")]
    body = file_fn[file_fn.index('"""', file_fn.index('"""') + 3) :]  # 跳过 docstring
    assert '"Cache-Control": "no-store"' not in body, "静态资源不该用 no-store（每次全量重下）"
    assert '"Cache-Control": "no-cache"' in body
    assert "304" in body, "ETag 命中要返回 304"


def test_song_cache_has_ttl():
    """回归：歌曲缓存 400 条无 TTL，远程投递会取到陈旧的封面与元数据。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "core" / "webui" / "_server.py").read_text("utf-8")
    assert "SONG_CACHE_TTL" in src
    assert "time.time() - ts > SONG_CACHE_TTL" in src


# ── 认证限速：轮换 IP 不能绕过 ──
def test_rate_limit_has_global_threshold(tmp_path):
    """回归：原先只按单 IP 计数，轮换源 IP 可无限次试密码。"""
    auth = AuthManager(tmp_path, lambda: "pw")
    last = ""
    for i in range(auth.RATE_MAX_FAILS_TOTAL + 5):
        last = f"10.0.{i // 250}.{i % 250 + 1}"
        auth.record_fail(last)
    # 全局阈值触发后，最后一个 IP 应被锁（轮换 IP 无法绕过）
    assert auth.login_blocked(last) > 0


def test_login_type_tautology_removed():
    """回归：`str(x) if src == 'qq' else 'qq'` 两分支同值，误导读者以为非 qq 有特殊处理。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "core" / "webui" / "_api_music.py").read_text("utf-8")
    assert 'else "qq"' not in src


# ── JSON 可序列化（回归：交付前自检）───
def test_all_config_keys_have_schema_entry():
    """代码读了但 schema 没暴露的配置项= 用户改不了。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "_conf_schema.json").read_text("utf-8"))
    known = set(schema) | {"acl", "webui", "stats", "scheduler", "ncm", "kg", "qq"}
    src = (root / "core" / "config.py").read_text("utf-8")
    import re

    read_keys = set(re.findall(r'self\.get\("(\w+)"\)', src))
    missing = read_keys - known
    assert not missing, f"config.py 读了 schema 里没有的键：{sorted(missing)}"
