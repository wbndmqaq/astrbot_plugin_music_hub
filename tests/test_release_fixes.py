"""2026-10-05 审查修复的回归钉（v1.0.0 重发布）。

每条对应一个已修复缺陷：修复退回时对应用例必须失败。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from astrbot_plugin_music_hub.core.errors import ApiError, TooManyTasks
from astrbot_plugin_music_hub.core.quality import KG_LADDER, kg_hash_for, ladder_for


# ── QQ 专辑搜索：AlbumSearch.singer 是字符串 ──
def _album_item(**kw):
    return type("A", (), kw)()


def test_norm_album_strips_em_from_string_singer():
    from astrbot_plugin_music_hub.core.api.qq import _norm_album

    item = _album_item(name="范特西", mid="002e5Lye3ZbT3", singer="<em>周杰伦</em>")
    out = _norm_album(item, 0)
    assert out["artist"] == "周杰伦", "字符串歌手（含 <em>）应去标签后直接用"


def test_norm_album_prefers_structured_singer_list():
    from astrbot_plugin_music_hub.core.api.qq import _norm_album

    item = _album_item(
        name="范特西",
        mid="002e5Lye3ZbT3",
        singer="高亮串",
        singer_list=[type("S", (), {"name": "周杰伦"})()],
    )
    out = _norm_album(item, 0)
    assert out["artist"] == "周杰伦"


def test_norm_album_list_singer_still_works():
    """基类 Album.singer 是 list[Singer]，详情/新专辑路径不能被字符串分支破坏。"""
    from astrbot_plugin_music_hub.core.api.qq import _norm_album

    item = _album_item(name="范特西", mid="x", singer=[type("S", (), {"name": "周杰伦"})()])
    out = _norm_album(item, 0)
    assert out["artist"] == "周杰伦"


# ── QQ 依赖缺失时的降级路径 ──
def test_qq_module_imports_without_qqmusic_api():
    """缺库时 import 不能 NameError（曾在 except 分支漏兜底 SearchType 等名字）。"""
    import os
    import subprocess
    import sys

    plugin_parent = Path(__file__).resolve().parents[1].parent
    framework = Path(os.environ.get("ASTRBOT_SRC", plugin_parent / "AstrBot111"))
    code = (
        "import sys\n"
        "sys.modules['qqmusic_api'] = None  # 阻断后续 import，使其抛 ImportError\n"
        "import astrbot_plugin_music_hub.core.api.qq as q\n"
        "assert q.available() is False\n"
        "assert q.SearchType is None and q.SongFileType is None\n"
        "assert 'qqmusic_api' in q.import_error()\n"
        "print('ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(framework), str(plugin_parent)])}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout, f"降级 import 失败：{r.stderr[-400:]}"


# ── 酷狗音质阶梯与 /song/url 端点能力匹配 ──
def test_kg_ladder_only_contains_song_url_qualities():
    """旧版 /song/url 只支持 flac/320/128（viper/super/high 属 /song/url/new）。

    往阶梯加回无效档会让每首歌先白打失败请求，此用例拦住。
    """
    assert set(KG_LADDER) <= {"flac", "320", "128"}, f"阶梯出现 /song/url 不支持的档位：{KG_LADDER}"
    assert ladder_for("kg", "auto") == KG_LADDER


def test_kg_hash_for_each_ladder_rung():
    song = {
        "hash": "h0",
        "hash_128": "h128",
        "hash_320": "h320",
        "hash_flac": "hflac",
        "hash_high": "hhigh",
    }
    assert kg_hash_for(song, "flac") == "hflac"
    assert kg_hash_for(song, "320") == "h320"
    assert kg_hash_for(song, "128") == "h128"


# ── run_listen_all 的让路语义 ──
def _listen_service(*, reason, owns):
    class S:
        played = []

        def check_song_request(self):
            return reason

        def scope(self, event):
            return "s"

        class sessions:
            @staticmethod
            async def owns_scope(scope):
                return owns

            @staticmethod
            async def songs_of(scope):
                return []

        async def play_all(self, event, songs):
            self.played.append(songs)

    return S()


async def test_listen_all_yields_when_song_request_disabled():
    from astrbot_plugin_music_hub.handlers.play import run_listen_all

    result = await run_listen_all(_listen_service(reason="已关闭", owns=True), object())
    assert result is False, "功能关闭时必须 return False 让路，否则事件被吞且无回复"


async def test_listen_all_yields_when_scope_owned_by_other():
    from astrbot_plugin_music_hub.handlers.play import run_listen_all

    result = await run_listen_all(_listen_service(reason=None, owns=False), object())
    assert result is False, "会话被其他音乐插件占用时必须 return False 让路"


# ── 点歌台 -3（后台任务超限回滚） ──
async def test_enqueue_reports_busy_on_minus_three():
    """queue.add 返回 -3（spawn 被并发上限拒绝、入队已回滚）时必须提示繁忙，
    不能掉进成功分支输出「第 -3 位」。"""
    from astrbot_plugin_music_hub.handlers.queue import run_enqueue

    class S:
        replies = []

        def check_song_request(self):
            return None

        config = type("C", (), {"cooldown_sec": 0})()

        async def check_cooldown(self, event):
            return None

        async def search_songs(self, keyword, limit=3):
            return [{"source": "ncm", "sid": "1", "name": "晴天", "artist": "周杰伦"}], "ncm"

        def scope(self, event):
            return "s"

        class queue:
            @staticmethod
            async def add(scope, song, requester):
                return -3

        async def reply(self, event, text):
            self.replies.append(text)

    svc = S()
    ev = type("E", (), {"message_str": "排队 晴天", "get_sender_name": lambda self: "用户"})()
    await run_enqueue(svc, ev)
    assert svc.replies, "-3 必须有回复"
    assert "-3" not in svc.replies[0], "不应把内部返回值暴露给用户"
    assert "繁忙" in svc.replies[0]


# ── 链接解析的冷却章 ──
def _resolve_service(release_calls):
    class S:
        def check_cooldown(self, event):
            return None  # 冷却通过

        def release_cooldown(self, event):
            release_calls.append(1)

        def log_warn(self, msg):
            pass

        async def reply(self, event, text):
            pass

    return S()


async def test_resolve_releases_cooldown_when_not_handled():
    """解析未接管消息时必须退还冷却章，否则会占用接下来的点歌冷却。"""
    from astrbot_plugin_music_hub.core.resolve import handle_resolve

    calls: list = []
    svc = _resolve_service(calls)
    handled = await handle_resolve(svc, object(), "跟音乐毫无关系的一句话")
    assert handled is False
    assert calls == [1], "未接管必须 release_cooldown"


async def test_resolve_gate_blocks_when_cooldown_active():
    """冷却进行中：解析直接拦下并回复，不 reply 上游、不误退冷却章。"""
    from astrbot_plugin_music_hub.core.resolve import handle_resolve

    released: list = []
    replied: list = []

    class S:
        async def check_cooldown(self, event):
            return "点歌冷却中，30 秒后再试"

        def release_cooldown(self, event):
            released.append(1)

        async def reply(self, event, text):
            replied.append(text)

    handled = await handle_resolve(S(), object(), "https://music.163.com/song?id=1")
    assert handled is True
    assert released == [], "冷却被拦时没有盖上章，不应调用 release"
    assert replied and "冷却" in replied[0]


# ── 版本号单一事实来源 ──
def test_version_is_consistent_across_sources():
    """版本漂移已复发三次：metadata.yaml、help_data.VERSION、CHANGELOG 顶部必须一致。"""
    import sys

    import yaml

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    meta = yaml.safe_load((Path(__file__).resolve().parents[1] / "metadata.yaml").read_text("utf-8"))
    from astrbot_plugin_music_hub.core import help_data

    assert meta["version"] == help_data.VERSION, (
        f"metadata.yaml({meta['version']}) 与 help_data.VERSION({help_data.VERSION}) 不一致"
    )
    changelog = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text("utf-8")
    m = re.search(r"^## (v[\d.]+)", changelog, re.MULTILINE)
    assert m, "CHANGELOG 缺少版本条目"
    assert m.group(1) == f"v{meta['version']}", (
        f"CHANGELOG 顶部是 {m.group(1)}，metadata 是 {meta['version']}"
    )


# ── 酷狗设备注册：并发锁与坏缓存守卫 ──
def _kg_client(tmp_path, monkeypatch, request_impl, jar="KUGOU_API_MID=abc"):
    from astrbot_plugin_music_hub.core.api.kg import KugouClient

    cfg = type("C", (), {"src_api_base": lambda self, s: "http://127.0.0.1:4000"})()
    client = KugouClient(cfg, tmp_path / "device_cookies.json")

    async def _request(path, params, inject_cookie=True):
        return await request_impl(path, params)

    monkeypatch.setattr(client, "request", _request)
    monkeypatch.setattr(client, "_jar_cookie", lambda: jar)
    return client


async def test_ensure_device_silent_failure_does_not_persist(tmp_path, monkeypatch):
    """注册被静默限频（无 dfid）时不落盘：缺 dfid 的缓存会让后续每个请求
    被服务端注入随机 dfid，放大风控。"""
    calls: list = []

    async def impl(path, params):
        calls.append(path)
        return {"data": {}}

    client = _kg_client(tmp_path, monkeypatch, impl)
    with pytest.raises(ApiError):
        await client.ensure_device()
    assert not (tmp_path / "device_cookies.json").exists(), "未拿到 dfid 不应落盘设备缓存"


async def test_ensure_device_concurrent_single_register(tmp_path, monkeypatch):
    calls: list = []

    async def impl(path, params):
        calls.append(path)
        await asyncio.sleep(0.02)
        return {"data": {"dfid": "DFID123"}}

    client = _kg_client(tmp_path, monkeypatch, impl)
    got = await asyncio.gather(client.ensure_device(), client.ensure_device(), client.ensure_device())
    assert len(calls) == 1, f"并发首调应只注册一次，实际 {len(calls)} 次"
    assert all("dfid=DFID123" in g for g in got)


async def test_spawn_rejects_over_limit(tmp_path=None):
    """TooManyTasks 是 -3 的上游信号，类型必须可被 handler 精确捕获。"""
    assert issubclass(TooManyTasks, Exception)
