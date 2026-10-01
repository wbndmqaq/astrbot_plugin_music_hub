"""酷狗 song 协议（编排下沉后的闭环验证）—— service 不再持有 per-source 分支。"""

import pytest
from astrbot_plugin_music_hub.core.api.kg import KugouClient

SONG = {"sid": "HASH1", "sid2": "42", "name": "晴天", "artist": "周杰伦"}


@pytest.fixture
def client(monkeypatch):
    c = KugouClient.__new__(KugouClient)  # 不触发 __init__（config/设备依赖与协议逻辑无关）

    async def fake_locate(self, song):
        return [{"id": "9", "accesskey": "AK"}]

    async def fake_lyric(self, cid, accesskey, fmt="lrc"):
        return f"content-in-{fmt}"

    monkeypatch.setattr(KugouClient, "_locate_candidates", fake_locate)
    monkeypatch.setattr(KugouClient, "lyric", fake_lyric)
    return c


async def test_song_lyric_returns_lrc(client):
    data = await client.song_lyric(SONG)
    assert data["lrc"] == "content-in-lrc"
    assert data["yrc"] == ""


async def test_song_lyric_karaoke_returns_krc(client):
    data = await client.song_lyric_karaoke(SONG)
    assert data["yrc"] == "content-in-krc"
    assert data["lrc"] == ""


async def test_song_lyric_without_accesskey_is_empty(client, monkeypatch):
    async def empty_locate(self, song):
        return [{"id": "", "accesskey": ""}]

    monkeypatch.setattr(KugouClient, "_locate_candidates", empty_locate)
    assert await client.song_lyric(SONG) == {"lrc": "", "tlyric": "", "yrc": ""}


async def test_song_comments_prefers_album_audio_id(client, monkeypatch):
    seen = {}

    async def fake_comments(self, mixsongid, limit=12, kind="music"):
        seen["id"] = mixsongid
        return {"hot": [], "new": [], "total": 0}

    monkeypatch.setattr(KugouClient, "comments", fake_comments)
    await client.song_comments(SONG)
    assert seen["id"] == "42"  # sid2（album_audio_id）优先
