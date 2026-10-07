"""AUDIT 2026-10-06 v3 音源 API 修复回归：QQ 取流阶梯错误归因、ncm 封面空值、
扩展名映射（杜比全景声/试听档）、kg 时长单位、热评打标等。"""

import pytest
from astrbot_plugin_music_hub.core import media
from astrbot_plugin_music_hub.core.api.http import num, opt_int
from astrbot_plugin_music_hub.core.api.kg import _dur as kg_dur
from astrbot_plugin_music_hub.core.api.kg import normalize_song as kg_song
from astrbot_plugin_music_hub.core.api.ncm import (
    NeteaseClient,
)
from astrbot_plugin_music_hub.core.api.ncm import (
    normalize_album as ncm_album,
)
from astrbot_plugin_music_hub.core.api.ncm import (
    normalize_artist as ncm_artist,
)
from astrbot_plugin_music_hub.core.api.ncm import (
    normalize_playlist as ncm_playlist,
)
from astrbot_plugin_music_hub.core.api.ncm import (
    normalize_song as ncm_song,
)
from astrbot_plugin_music_hub.core.api.qq import QQClient
from astrbot_plugin_music_hub.core.errors import ApiError
from astrbot_plugin_music_hub.core.quality import QQ_LADDER
from qqmusic_api.core.exceptions import CredentialExpiredError, NetworkError, RatelimitedError


class _NoUnblockConfig:
    """song_url_best 只读 src_quality_unblock：测试统一关闭试听兜底，聚焦阶梯归因。"""

    def src_quality_unblock(self, source: str) -> bool:
        return False


def _qq_client() -> QQClient:
    # __new__ 绕过 __init__：取流归因逻辑只依赖 _config 与 _url_for，均可注入
    c = QQClient.__new__(QQClient)
    c._config = _NoUnblockConfig()
    return c


# ── 1. QQ 取流阶梯的错误归因 ──
async def test_song_url_best_credential_expired_maps_to_relogin(monkeypatch):
    """凭证过期是账号级故障：抛「重新扫码登录」语义的 ApiError，而非「无版权」兜底。"""
    calls = []

    async def expired(self, mid, ftype):
        calls.append(ftype)
        raise CredentialExpiredError(code=700)

    monkeypatch.setattr(QQClient, "_url_for", expired)
    with pytest.raises(ApiError) as ei:
        await _qq_client().song_url_best({"sid": "MID"}, "auto")
    assert "重新扫码登录" in ei.value.message
    assert "无版权" not in ei.value.message
    assert len(calls) == 1  # 首档即抛，不再逐档降档


async def test_song_url_best_ratelimited_stops_ladder(monkeypatch):
    """风控同样提前终止：一次尝试即抛，不把 7 档全打一遍。"""
    calls = []

    async def limited(self, mid, ftype):
        calls.append(ftype)
        raise RatelimitedError(code=2001)

    monkeypatch.setattr(QQClient, "_url_for", limited)
    with pytest.raises(ApiError) as ei:
        await _qq_client().song_url_best({"sid": "MID"}, "auto")
    assert "风控" in ei.value.message
    assert len(calls) == 1


async def test_song_url_best_transient_error_falls_through_and_attributes(monkeypatch):
    """普通异常保留降档语义：逐档试到底，最终归因 last_exc（网络异常）而非「无版权」。"""
    calls = []

    async def flaky(self, mid, ftype):
        calls.append(ftype)
        raise NetworkError("boom")

    monkeypatch.setattr(QQClient, "_url_for", flaky)
    with pytest.raises(ApiError) as ei:
        await _qq_client().song_url_best({"sid": "MID"}, "auto")
    expected = sum(1 for _q, primary, fallback in QQ_LADDER for name in (primary, fallback) if name)
    assert len(calls) == expected  # 整条阶梯都试过（降档语义未变）
    assert "网络异常" in ei.value.message
    assert "无版权" not in ei.value.message


async def test_song_url_best_code_only_failure_keeps_vip_copy(monkeypatch):
    """「有 code 没异常」的路径保留 last_code 文案：104003 → VIP/登录提示。"""

    async def fail(self, mid, ftype):
        return {"ok": False, "code": 104003, "url": ""}

    monkeypatch.setattr(QQClient, "_url_for", fail)
    with pytest.raises(ApiError) as ei:
        await _qq_client().song_url_best({"sid": "MID"}, "auto")
    assert "VIP" in ei.value.message


# ── 2. ncm 封面拼接空值安全 ──
def test_ncm_cover_missing_field_yields_empty_string():
    """字段缺失时 cover 是空串，而不是只有 ?param= 的垃圾地址。"""
    assert ncm_song({"id": 1, "name": "x", "album": {"name": "al"}})["cover"] == ""
    assert ncm_playlist({"id": 1, "name": "x"})["cover"] == ""
    assert ncm_album({"id": 1, "name": "x"})["cover"] == ""
    assert ncm_artist({"id": 1, "name": "x"})["cover"] == ""


def test_ncm_cover_present_keeps_size_param():
    song = ncm_song({"id": 1, "name": "x", "album": {"name": "a", "picUrl": "http://p/1.jpg"}})
    assert song["cover"] == "http://p/1.jpg?param=300y300"


# ── 3. ncm daily_signin 带 timestamp 穿透 2 分钟缓存 ──
async def test_ncm_daily_signin_carries_timestamp(monkeypatch):
    c = NeteaseClient.__new__(NeteaseClient)
    seen = {}

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        seen["path"], seen["params"] = pathname, params
        return {"code": 200}

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    assert await c.daily_signin() == "签到成功"
    assert seen["params"].get("timestamp")


# ── 3b. 重复签到的 HTTP 400 嵌套 code=-2（{android:{code}} 无顶层 code） ──
async def test_ncm_daily_signin_duplicate_via_nested_payload(monkeypatch):
    """回归：HTTP 400 路径上 _handle 只提取到顶层 code（恒 None），-2 必须从 payload 解嵌套。"""
    c = NeteaseClient.__new__(NeteaseClient)

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        raise ApiError(
            "请求失败（HTTP 400）",
            source="ncm",
            payload={"android": {"code": -2, "msg": "重复签到"}, "web": {"code": 0}},
        )

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    assert await c.daily_signin() == "今天已经签到过啦"


async def test_ncm_daily_signin_other_error_still_surfaces(monkeypatch):
    c = NeteaseClient.__new__(NeteaseClient)

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        raise ApiError("请求失败（HTTP 503）", source="ncm", payload={})

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    assert await c.daily_signin() == "请求失败（HTTP 503）"


# ── 3c. personal_fm 解包 mainSong/mainMusic（/api/v1/radio/get 的 data[] 是节目对象） ──
async def test_ncm_personal_fm_unwraps_mainsong(monkeypatch):
    """回归：包裹结构直接归一化会全部丢弃 → 私人 FM 恒返回空、静默回退推荐新歌。"""
    c = NeteaseClient.__new__(NeteaseClient)

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        return {
            "data": [
                {"mainSong": {"id": 1, "name": "晴天", "ar": [{"name": "周杰伦"}]}},
                {"mainMusic": {"id": 2, "name": "七里香", "ar": [{"name": "周杰伦"}]}},
            ]
        }

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    songs = await c.personal_fm()
    assert [s["name"] for s in songs] == ["晴天", "七里香"]


async def test_ncm_personal_fm_flat_shape_still_works(monkeypatch):
    """平铺形状（元素本身就是歌曲）保持兼容，解包链原样透传。"""
    c = NeteaseClient.__new__(NeteaseClient)

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        return {"data": [{"id": 3, "name": "稻香", "ar": [{"name": "周杰伦"}]}]}

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    songs = await c.personal_fm()
    assert [s["name"] for s in songs] == ["稻香"]


# ── 3d. recent_songs 层级容错：resourceList 包裹与 data 直取歌曲两种形态 ──
async def test_ncm_recent_songs_accepts_both_shapes(monkeypatch):
    """上游层级随版本漂移：老结构取不到时按歌曲字段探测 data 本身，两条路都出歌。"""
    c = NeteaseClient.__new__(NeteaseClient)

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        return {
            "data": {
                "list": [
                    {
                        "playTime": 1700000000000,
                        "data": {"resourceList": {"songInfo": {"id": 1, "name": "包裹形"}}},
                    },
                    {"playTime": 1700000000000, "data": {"id": 2, "name": "平铺形"}},
                ]
            }
        }

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    songs = await c.recent_songs()
    assert [s["name"] for s in songs] == ["包裹形", "平铺形"]


# ── 4. ncm 热评按来源打标（hotComments 条目本无 hotComment 字段） ──
async def test_ncm_hot_comment_flag_comes_from_source(monkeypatch):
    c = NeteaseClient.__new__(NeteaseClient)

    async def fake_request(self, pathname, params=None, *, with_cookie=True):
        return {
            "hotComments": [{"content": "热评", "user": {"nickname": "a"}}],
            "comments": [{"content": "新评", "user": {"nickname": "b"}}],
            "total": 2,
        }

    monkeypatch.setattr(NeteaseClient, "request", fake_request)
    res = await c.comments("1")
    assert res["hot"][0]["hot"] is True
    assert res["new"][0]["hot"] is False


# ── 6. kg _dur 显式毫秒（<10s 音频不再格式化成 150:00） ──
def test_kg_dur_explicit_ms_fixes_short_clip():
    assert kg_dur(9000, "ms") == "00:09"
    assert kg_dur(240000, "ms") == "04:00"
    assert kg_dur(258) == "04:18"  # auto：秒值
    assert kg_dur(240000) == "04:00"  # auto：毫秒值


def test_kg_normalize_song_short_timelength():
    s = kg_song({"name": "x", "hash": "h", "audio_info": {"timelength": 9000}})
    assert s["duration"] == "00:09"
    assert s["dtMs"] == 9000


def test_kg_normalize_song_falls_back_to_search_duration():
    s = kg_song({"name": "x", "hash": "h", "Duration": 258})
    assert s["duration"] == "04:18"


# ── 10. num 与 opt_int 对 bool 语义一致 ──
def test_num_and_opt_int_agree_on_bool():
    assert num(True) == 1
    assert num(False) == 0
    assert opt_int(True) == 1


# ── 12. 扩展名映射对照上游枚举 ──
def test_ext_atmos_db_and_dolby_are_mp4():
    """SongFileType.ATMOS_DB=("D004",".mp4")：杜比全景声是 mp4 容器，atmos/master 仍 flac。"""
    assert media._ext_for_quality("atmos_db", "") == ".mp4"
    assert media._ext_for_quality("dolby", "") == ".mp4"
    assert media._ext_for_quality("atmos", "") == ".flac"
    assert media._ext_for_quality("master", "") == ".flac"


def test_ext_qq_trial_rs02_is_mp3():
    """SpecialSongFileType.TRY=("RS02",".mp3")：试听档落 .mp3，无扩展名 url 也不例外。"""
    assert media._ext_for_quality("try", "https://isure.stream.qqmusic.qq.com/RS02abc123.mp3") == ".mp3"
    assert media._ext_for_quality("try", "https://isure.stream.qqmusic.qq.com/RS02abc123") == ".mp3"
