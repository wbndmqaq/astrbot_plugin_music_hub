"""投递与媒体链路的行为测试（此前完全无覆盖）。

覆盖三块高风险路径：
- deliver_song 的通道选择决策表（sendVocal/uploadFile/平台能力 → 实际通道）
- 发送失败 → 压缩重试 → 文本兜底的降级链（remote.send_audio_to / deliver_video）
- _send_file 直发失败后文案不丢（回归：pending.take() 过早清空）

外部依赖（下载、ffmpeg、临时目录、OneBot 直发、平台能力）全部在
delivery/remote 模块命名空间内替换，不触网。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrbot_plugin_music_hub.core import delivery as delivery_mod
from astrbot_plugin_music_hub.core import remote as remote_mod
from astrbot_plugin_music_hub.core.config import Config

SONG = {"source": "ncm", "sid": "1", "name": "晴天", "artist": "周杰伦", "album": "叶惠美"}
PLAY = {"url": "http://example.com/a.mp3", "quality": "320", "qualityLabel": "高品 320K"}


class FakeService:
    def __init__(self, cfg: dict | None = None):
        self.config = Config(cfg or {})
        self.chains = []  # 每次 send_chain 的组件元组
        self.sent = []  # context.send_message 的 (umo, chain)

        class _Ctx:
            async def send_message(_self, umo, chain):
                self.sent.append((umo, chain))

        self.context = _Ctx()

    async def send_chain(self, event, *comps):
        self.chains.append(comps)

    def plain(self, text):
        return SimpleNamespace(text=text)

    async def resolve_play(self, song):
        return dict(PLAY)


def full_caps(**kw):
    base = {"vocal": True, "file": True, "native_card": False, "passive_limited": False, "notes": {}}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def media_mocks(tmp_path, monkeypatch):
    """把 delivery/remote 的媒体外呼全部换成假件。"""
    local = tmp_path / "a.mp3"
    local.write_bytes(b"fake")
    mocked = {"local": local, "cleaned": [], "record_direct": [], "file_direct": []}

    async def fake_dl(*a, **kw):
        return {"filePath": str(local)}

    async def fake_prep(path, **kw):
        return path

    async def fake_temp():
        return tmp_path

    monkeypatch.setattr(delivery_mod, "download_audio", fake_dl)
    monkeypatch.setattr(delivery_mod, "prepare_vocal_file", fake_prep)
    monkeypatch.setattr(delivery_mod, "get_temp_dir", fake_temp)
    monkeypatch.setattr(delivery_mod, "schedule_cleanup", lambda p, s: mocked["cleaned"].append(p))
    monkeypatch.setattr(remote_mod, "download_audio", fake_dl)
    monkeypatch.setattr(remote_mod, "prepare_vocal_file", fake_prep)
    monkeypatch.setattr(remote_mod, "get_temp_dir", fake_temp)
    monkeypatch.setattr(remote_mod, "schedule_cleanup", lambda p, s: mocked["cleaned"].append(p))
    return mocked


# ── 通道选择决策表 ──
async def test_both_channels_off_means_no_channel(media_mocks):
    svc = FakeService({"sendVocal": False, "uploadFile": False})
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY)
    assert r == {"ok": False, "reason": "no_channel", "downloaded": False}
    assert not svc.chains or all("下载" not in str(c) for c in svc.chains)


async def test_voice_only_platform_sends_record(media_mocks):
    svc = FakeService({"uploadFile": False})
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY, options={"skipTextInfo": True})
    assert r["ok"] is True
    kinds = [type(c).__name__ for comp in svc.chains for c in comp]
    assert "Record" in kinds and "File" not in kinds


async def test_file_only_platform_sends_file(media_mocks):
    svc = FakeService({"sendVocal": False})
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY, options={"skipTextInfo": True})
    assert r["ok"] is True
    kinds = [type(c).__name__ for comp in svc.chains for c in comp]
    assert "File" in kinds and "Record" not in kinds


async def test_platform_without_vocal_but_file_on_sends_file(media_mocks, monkeypatch):
    monkeypatch.setattr(delivery_mod, "caps_of", lambda e: full_caps(vocal=False))
    svc = FakeService()
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY, options={"skipTextInfo": True})
    assert r["ok"] is True
    kinds = [type(c).__name__ for comp in svc.chains for c in comp]
    assert "File" in kinds and "Record" not in kinds


async def test_platform_without_media_and_both_on_gives_notice(media_mocks, monkeypatch):
    """语音文件都开了但平台两者都不支持：发说明文案而不是静默。"""
    monkeypatch.setattr(delivery_mod, "caps_of", lambda e: full_caps(vocal=False, file=False))
    svc = FakeService()
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY, options={"skipTextInfo": True})
    assert r["ok"] is False and r["reason"] == "send_fail"
    assert any("不支持语音" in str(c) for comp in svc.chains for c in comp)


# ── 下载失败链 ──
async def test_download_fail_reports_and_no_media(media_mocks, monkeypatch):
    async def boom(*a, **kw):
        raise OSError("net down")

    monkeypatch.setattr(delivery_mod, "download_audio", boom)
    svc = FakeService()
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY, options={"skipTextInfo": True})
    assert r["ok"] is False and r["reason"] == "download_fail"
    assert any("下载音频失败" in str(c) for comp in svc.chains for c in comp)


# ── _send_file：直发失败后文案不丢（回归 #23）──
async def test_file_direct_send_failure_keeps_caption(media_mocks, monkeypatch):
    async def direct_fail(event, text, name, path):
        raise RuntimeError("onebot down")

    monkeypatch.setattr(delivery_mod, "caps_of", lambda e: full_caps(native_card=True))
    monkeypatch.setattr(delivery_mod, "aiocq_send_file", direct_fail)
    svc = FakeService({"sendVocal": False})
    r = await delivery_mod.deliver_song(svc, None, SONG, PLAY)
    assert r["ok"] is True
    # 组件通道发出的 File 必须带着「识别：」前缀文案（直发失败时 take 过早会丢）
    file_calls = [comp for c in svc.chains for comp in c if type(comp).__name__ == "File"]
    assert file_calls, "应退回 File 组件通道"
    captions = [comp.text for c in svc.chains for comp in c if hasattr(comp, "text") and comp.text]
    assert any("识别" in t for t in captions), "降级发送的文件必须带识别前缀文案"


# ── remote.send_audio_to 的文本兜底语义（回归 #27）──
async def test_remote_text_fallback_counts_as_ok(media_mocks, monkeypatch):
    async def boom(*a, **kw):
        raise OSError("net down")

    monkeypatch.setattr(remote_mod, "download_audio", boom)
    monkeypatch.setattr(remote_mod, "caps_for_name", lambda name: full_caps())
    svc = FakeService()
    r = await remote_mod.send_audio_to(svc, "aiocqhttp:GroupMessage:1", SONG, PLAY)
    assert r == {"ok": True, "reason": "text_fallback"}
    # 用户确实收到了带直链的文本
    assert svc.sent and "http://example.com/a.mp3" in str(svc.sent[0][1])


async def test_remote_no_url_is_still_failure(media_mocks, monkeypatch):
    monkeypatch.setattr(remote_mod, "caps_for_name", lambda name: full_caps())
    svc = FakeService()

    async def resolve_empty(song):
        return {"url": ""}

    svc.resolve_play = resolve_empty
    r = await remote_mod.send_audio_to(svc, "aiocqhttp:GroupMessage:1", SONG, {"url": ""})
    assert r["ok"] is False and r["reason"] == "no_url"


async def test_remote_text_fallback_send_fails_keeps_fail_reason(media_mocks, monkeypatch):
    """文本兜底也失败时必须按失败记（不能谎报成功）。"""
    monkeypatch.setattr(remote_mod, "caps_for_name", lambda name: full_caps(vocal=False, file=False))

    class _Ctx:
        async def send_message(self, umo, chain):
            raise RuntimeError("all down")

    svc = FakeService()
    svc.context = _Ctx()
    r = await remote_mod.send_audio_to(svc, "aiocqhttp:GroupMessage:1", SONG, PLAY)
    assert r["ok"] is False and r["reason"] == "send_fail"


# ── deliver_video 下载失败的兜底链（回归 #24）──
async def test_video_download_fail_falls_back_to_url_send(media_mocks, monkeypatch):
    async def boom(*a, **kw):
        raise OSError("net down")

    monkeypatch.setattr(delivery_mod, "download_audio", boom)
    monkeypatch.setattr(delivery_mod, "caps_of", lambda e: full_caps())
    svc = FakeService()
    r = await delivery_mod.deliver_video(svc, None, {"name": "MV"}, "http://example.com/v.mp4", download=True)
    assert r["ok"] is True and r["reason"] == "url"


async def test_video_all_channels_fail_sends_text(media_mocks, monkeypatch):
    async def boom(*a, **kw):
        raise OSError("net down")

    monkeypatch.setattr(delivery_mod, "download_audio", boom)
    monkeypatch.setattr(delivery_mod, "caps_of", lambda e: full_caps())

    async def chain_fail(event, *comps):
        if any(type(c).__name__ == "Video" for c in comps):
            raise RuntimeError("video down")

    svc = FakeService()
    svc.send_chain = chain_fail
    r = await delivery_mod.deliver_video(svc, None, {"name": "MV"}, "http://example.com/v.mp4", download=True)
    assert r["ok"] is True and r["reason"] == "text_fallback"
