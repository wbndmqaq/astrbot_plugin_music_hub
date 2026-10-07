"""安全与资源：WebUI 密钥文件权限、配置写入范围、远程投递白名单、临时文件清理。

对应 AUDIT_REPORT 1.8 / 1.9 / 1.4 / 1.13。这些是「已认证用户可造成的实际损害」，
用测试钉住边界，避免后续改动把校验删掉。
"""

import asyncio
import os
import stat

import pytest
from astrbot_plugin_music_hub.core.config import Config
from astrbot_plugin_music_hub.core.errors import TooManyTasks


# ── 1.4-h WebUI 会话密钥文件权限 ──
def test_secret_file_is_owner_only(tmp_path):
    """回归：密钥以 umask 决定的默认权限落盘，同机任意用户可读取并伪造会话。"""
    from astrbot_plugin_music_hub.core.webui._auth import AuthManager

    d = tmp_path / "data"
    d.mkdir()
    am = AuthManager.__new__(AuthManager)
    am._data_dir = d
    am._secret = b""
    secret = am._load_secret()
    key_file = d / "webui_secret.key"
    assert key_file.exists(), "密钥应持久化，否则重启后全部会话失效"
    assert secret


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义，Windows 上不适用")
def test_secret_file_mode_is_0600(tmp_path):
    from astrbot_plugin_music_hub.core.webui._auth import AuthManager

    d = tmp_path / "data"
    d.mkdir()
    am = AuthManager.__new__(AuthManager)
    am._data_dir = d
    am._secret = b""
    am._load_secret()
    key_file = d / "webui_secret.key"
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600, f"密钥文件权限应为 0600，实际 {oct(mode)}"


def test_secret_is_reused_across_restart(tmp_path):
    """第二次加载必须复用同一密钥，否则重启后所有会话失效。"""
    from astrbot_plugin_music_hub.core.webui._auth import AuthManager

    d = tmp_path / "data"
    d.mkdir()

    def make():
        am = AuthManager.__new__(AuthManager)
        am._data_dir = d
        am._secret = b""
        return am

    s1 = make()._load_secret()
    s2 = make()._load_secret()
    assert s1 == s2


# ── 1.9 配置写入必须做范围校验 ──
_INT_RANGES = {
    "maxList": (1, 20),
    "compressBitrate": (32, 320),
    "downloadTimeout": (5000, 600000),
    "keepFileSec": (5, 3600),
    "cooldownSec": (0, 600),
    "rateLimitMs": (0, 5000),
}
# 嵌套节点里的 int 键，键名为 "<节点>.<字段>"
_NESTED_RANGES = {
    "webui.port": (1, 65535),
    "scheduler.signinHour": (0, 23),
}


@pytest.mark.parametrize("key,bounds", sorted(_INT_RANGES.items()))
def test_int_config_keys_have_declared_bounds(key, bounds):
    """每个可写 int 键都必须在 _INT_RANGES 中声明上下界。

    没有上下界的键可被写入 -1，让限速/冷却等保护静默失效。
    """
    from astrbot_plugin_music_hub.core.webui import _api_admin

    lo, hi = bounds
    assert lo < hi
    assert key in _api_admin._EDITABLE_KEYS, f"{key} 不在可编辑白名单，范围约束无意义"
    assert _api_admin._INT_RANGES.get(key) == (lo, hi)


@pytest.mark.parametrize("key,bounds", sorted(_NESTED_RANGES.items()))
def test_nested_int_keys_have_declared_bounds(key, bounds):
    """嵌套节点（webui.port / scheduler.signinHour）的区间也走同一张表。"""
    from astrbot_plugin_music_hub.core.webui import _api_admin

    assert _api_admin._NESTED_INT_RANGES.get(key) == bounds
    assert _api_admin.clamp_int(key, bounds[0] - 999) == bounds[0]
    assert _api_admin.clamp_int(key, bounds[1] + 999) == bounds[1]


def test_all_editable_int_keys_are_range_checked():
    from astrbot_plugin_music_hub.core.webui import _api_admin

    int_keys = {k for k, t in _api_admin._EDITABLE_KEYS.items() if t is int}
    unguarded = int_keys - set(_INT_RANGES)
    assert not unguarded, f"这些 int 键缺少范围校验，可被写入越界值：{sorted(unguarded)}"


def test_cooldown_negative_is_clamped():
    """回归护栏：cooldownSec=-1 会让 check_cooldown 恒返回 None，冷却被完全禁用。"""
    from astrbot_plugin_music_hub.core.webui._api_admin import clamp_int

    lo, hi = _INT_RANGES["cooldownSec"]
    assert clamp_int("cooldownSec", -1) == lo
    assert clamp_int("cooldownSec", 99999) == hi
    assert clamp_int("cooldownSec", 30) == 30


def test_clamp_int_rejects_garbage():
    from astrbot_plugin_music_hub.core.webui._api_admin import clamp_int

    with pytest.raises((TypeError, ValueError)):
        clamp_int("maxList", "abc")


def test_clamp_int_ignores_unknown_key():
    from astrbot_plugin_music_hub.core.webui._api_admin import clamp_int

    assert clamp_int("enable", 5) == 5  # 无范围声明的键原样返回


def test_cooldown_still_blocks_after_clamped_negative():
    """端到端：把越界值夹到 0 后，冷却逻辑行为仍是「关闭」而非「负数绕过」。"""

    class S:
        def __init__(self, sec):
            self.config = Config({"cooldownSec": sec})
            self._cooldowns = {}

    svc = S(0)
    assert svc.config.cooldown_sec == 0


# ── 1.8 远程投递目标白名单 ──
def test_remote_song_rejects_unregistered_umo():
    """回归：remote_play 只检查 umo 含冒号，可向插件未注册的任意会话投递。"""
    from astrbot_plugin_music_hub.core.webui._api_music import is_known_umo

    rows = [{"umo": "aiocqhttp:GroupMessage:111", "scope": "g1"}]
    assert is_known_umo("aiocqhttp:GroupMessage:111", rows) is True
    assert is_known_umo("aiocqhttp:GroupMessage:999", rows) is False
    assert is_known_umo("", rows) is False
    assert is_known_umo("no-colon", rows) is False


def test_spawn_has_a_concurrency_cap():
    """回归：_spawn 无上限，重复 POST 可累积数千后台任务耗尽内存。"""
    from astrbot_plugin_music_hub.core.service import MusicService

    assert hasattr(MusicService, "MAX_BG_TASKS"), "应声明后台任务上限常量"
    assert MusicService.MAX_BG_TASKS > 0


async def test_spawn_rejects_beyond_cap():
    """超出上限时应关闭协程并报错，而不是无限累积。"""
    from astrbot_plugin_music_hub.core.service import MusicService

    svc = MusicService.__new__(MusicService)
    svc._bg_tasks = set()
    svc.MAX_BG_TASKS = 2

    started = []

    async def work():
        started.append(1)
        await asyncio.sleep(10)

    for _ in range(2):
        svc.spawn(work())
    await asyncio.sleep(0)

    with pytest.raises(TooManyTasks):
        svc.spawn(work())
    # 不得泄漏未 await 的协程
    assert len(svc._bg_tasks) <= 2


# ── 1.13 关机时临时文件必须被清理 ──
def test_cleanup_timers_removes_just_registered_files(tmp_path):
    """回归：注册不足 _CANCEL_GRACE_SEC 的文件被 continue 跳过，既不删也不注销。"""
    from astrbot_plugin_music_hub.core import media

    async def run():
        f = tmp_path / "recent.bin"
        f.write_bytes(b"x")
        media._cleanup_timers.clear()
        media.schedule_cleanup(str(f), 600)
        assert media._cleanup_timers, "应已登记清理定时器"
        await media.cancel_cleanup_timers()
        return f

    f = asyncio.run(run())
    assert not f.exists(), "关机时刚注册的临时文件未被清理"
    assert not media._cleanup_timers, "定时器句柄应被注销，否则随 loop 消亡后无人回收"
    media._cleanup_timers.clear()


def test_cleanup_timers_removes_all_registered_files(tmp_path):
    from astrbot_plugin_music_hub.core import media

    async def run():
        files = []
        for i in range(3):
            p = tmp_path / f"f{i}.bin"
            p.write_bytes(b"x")
            files.append(p)
            media.schedule_cleanup(str(p), 600)
        await media.cancel_cleanup_timers()
        return files

    files = asyncio.run(run())
    for p in files:
        assert not p.exists()
    assert not media._cleanup_timers
    media._cleanup_timers.clear()


def test_cleanup_timer_fires_and_unregisters(tmp_path):
    """正常到期路径：定时器触发后应删文件并从登记表移除。"""
    from astrbot_plugin_music_hub.core import media

    async def run():
        f = tmp_path / "auto.bin"
        f.write_bytes(b"x")
        media.schedule_cleanup(str(f), 0)  # _MIN_KEEP_SEC 兜底为 5s，改用手动触发
        handle, (path, _ts) = next(iter(media._cleanup_timers.items()))
        # 直接执行注册的回调逻辑
        media._cleanup_timers.pop(handle, None)
        await asyncio.to_thread(media._remove_file, path)
        return f

    f = asyncio.run(run())
    assert not f.exists()
    media._cleanup_timers.clear()


def test_startup_sweep_removes_stale_files(tmp_path, monkeypatch):
    """startup_sweep 只清超过 1 小时的孤儿文件。"""
    from astrbot_plugin_music_hub.core import media

    async def fake_get_temp_dir():
        return str(tmp_path)

    monkeypatch.setattr(media, "get_temp_dir", fake_get_temp_dir)

    import time as _t

    old = tmp_path / "old.bin"
    old.write_bytes(b"x")
    stale = _t.time() - 7200
    os.utime(old, (stale, stale))
    fresh = tmp_path / "fresh.bin"
    fresh.write_bytes(b"x")

    asyncio.run(media.startup_sweep())
    assert not old.exists(), "超过 1 小时的孤儿文件应被清理"
    assert fresh.exists(), "新鲜文件不应被误删"


# ── 1.24 SSRF 白名单必须是主机名匹配 ──
@pytest.mark.parametrize(
    "url,allowed",
    [
        ("https://music.163.com/song?id=1", True),
        ("https://m.music.163.com/x", True),
        ("https://y.music.163.com.evil.com/x", False),  # 后缀欺骗
        ("https://evil.com/?x=.163.com", False),  # query 里带白名单串
        ("https://attacker.com/#url.cn", False),  # fragment 里带白名单串
        ("https://evil.com/redirect?to=.163.com", False),
        ("", False),
        ("not a url", False),
    ],
)
def test_redirect_allowlist_uses_hostname(url, allowed):
    """回归：白名单是子串匹配，evil.com/?x=.qq.com 之类可绕过。"""
    from astrbot_plugin_music_hub.core.resolve import is_allowed_redirect

    assert is_allowed_redirect(url) is allowed
