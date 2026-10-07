"""音源客户端协议契约：三个音源必须返回同构数据（回归 P0-1 / P0-2）。"""

import json
from pathlib import Path

import pytest
from astrbot_plugin_music_hub.core.api.qq import QQClient
from astrbot_plugin_music_hub.core.config import Config

# 统一歌曲结构：service / cards / WebUI 全部按这些键取值
SONG_KEYS = {
    "source",
    "sid",
    "name",
    "artist",
    "album",
    "cover",
    "duration",
    "dtMs",
    "pay",
    "trial",
}


class _Info:
    def __init__(self, result: int, purl: str):
        self.result = result
        self.purl = purl


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeSongApi:
    def __init__(self, resp):
        self._resp = resp

    async def get_song_urls(self, _items):
        return self._resp


class _FakeClient:
    def __init__(self, resp):
        self.song = _FakeSongApi(resp)


@pytest.fixture
def qq_client():
    """绕开 __init__（依赖 config/设备指纹），只测取流判定逻辑。"""
    return QQClient.__new__(QQClient)


async def test_qq_url_result_zero_is_success(qq_client, monkeypatch):
    """回归 P0-1：UrlinfoItem.result 成功值是 0，`or` 兜底会把它短路成 -1。

    旧写法 `int(getattr(info, "result", -1) or -1)` 让所有成功取流被判失败，
    QQ 音源整体不可用且报错文案误导为"无版权"。
    """
    resp = _Resp([_Info(0, "path/to/file.mp3?guid=x&vkey=y")])

    async def fake_get_client():
        return _FakeClient(resp)

    monkeypatch.setattr(qq_client, "get_client", fake_get_client)

    r = await qq_client._url_for("mid123", object())
    assert r["ok"] is True, "result=0 必须判为成功"
    assert r["code"] == 0
    assert r["url"].startswith("path/to/")


async def test_qq_url_result_permission_denied(qq_client, monkeypatch):
    """result=104003（无权限）必须判失败，且错误码原样透出。"""
    resp = _Resp([_Info(104003, "")])

    async def fake_get_client():
        return _FakeClient(resp)

    monkeypatch.setattr(qq_client, "get_client", fake_get_client)

    r = await qq_client._url_for("mid123", object())
    assert r["ok"] is False
    assert r["code"] == 104003


async def test_qq_url_empty_data_is_failure(qq_client, monkeypatch):
    """上游返回空 data 时不得抛异常，按失败返回。"""
    resp = _Resp([])

    async def fake_get_client():
        return _FakeClient(resp)

    monkeypatch.setattr(qq_client, "get_client", fake_get_client)

    r = await qq_client._url_for("mid123", object())
    assert r == {"ok": False, "code": None, "url": ""}


def test_qq_cookie_key_matches_schema():
    """回归 P0-2：Schema 声明的键名必须与 Config 读写的键名一致。

    AstrBot 的 check_config_integrity 会删除 Schema 中不存在的键；
    旧版 qq 节点用 credential/uin，而代码读写 cookie/uid，导致登录态每次重载即丢。
    """
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text("utf-8"))
    for src in ("ncm", "kg", "qq"):
        items = set(schema[src]["items"])
        assert "cookie" in items, f"{src}.cookie 未在 Schema 中声明"
        assert "uid" in items, f"{src}.uid 未在 Schema 中声明"


def test_qq_config_reads_legacy_credential_key():
    """旧配置文件用 credential/uin，读取时需兼容，避免升级后要求重新扫码。"""
    cfg = Config({"qq": {"credential": '{"musickey":"Q_H_L_x"}', "uin": "12345"}})
    assert cfg.src_cookie("qq") == '{"musickey":"Q_H_L_x"}'
    assert cfg.src_uid("qq") == "12345"


def test_qq_config_prefers_new_key():
    """新键 cookie/uid 优先于旧别名。"""
    cfg = Config({"qq": {"cookie": "new", "uid": "1", "credential": "old", "uin": "2"}})
    assert cfg.src_cookie("qq") == "new"
    assert cfg.src_uid("qq") == "1"


def test_qq_config_empty_when_both_absent():
    cfg = Config({"qq": {}})
    assert cfg.src_cookie("qq") == ""
    assert cfg.src_uid("qq") == ""


# ── 统一协议：三端必须实现同一组 song_* 方法（service 不再做平台分支）──

PROTOCOL_METHODS = (
    "song_url_best",
    "song_mv_url",
    "song_lyric",
    "song_lyric_karaoke",
    "song_comments",
)


@pytest.mark.parametrize(
    ("module", "cls_name"),
    [
        ("astrbot_plugin_music_hub.core.api.ncm", "NeteaseClient"),
        ("astrbot_plugin_music_hub.core.api.kg", "KugouClient"),
        ("astrbot_plugin_music_hub.core.api.qq", "QQClient"),
    ],
)
def test_all_sources_implement_protocol(module, cls_name):
    """回归 P1-14：service.fetch_mv_url / resolve_play 依赖这组方法做零分支调度。"""
    import importlib

    cls = getattr(importlib.import_module(module), cls_name)
    for name in PROTOCOL_METHODS:
        assert hasattr(cls, name), f"{cls_name} 缺少协议方法 {name}"


async def test_ncm_song_url_requires_sid():
    """统一签名后，缺 sid 需给出明确错误而非 KeyError。"""
    from astrbot_plugin_music_hub.core.api.ncm import NeteaseClient
    from astrbot_plugin_music_hub.core.errors import ApiError

    client = NeteaseClient.__new__(NeteaseClient)
    client._config = Config({"ncm": {}})

    with pytest.raises(ApiError, match="缺少 id"):
        await client.song_url_best({"name": "无 id"}, "auto")


async def test_ncm_song_url_bypasses_cache():
    """回归 P1-3：song_url 必须拼 timestamp，否则命中 api-enhanced 2 分钟缓存拿到失效 url。"""
    from astrbot_plugin_music_hub.core.api.ncm import NeteaseClient

    client = NeteaseClient.__new__(NeteaseClient)
    client._config = Config({"ncm": {}})

    captured = {}

    async def fake_request(path, params=None, **kw):
        captured["path"] = path
        captured["params"] = dict(params or {})
        return {"data": [{"url": "https://cdn/song.mp3", "level": "standard"}]}

    client.request = fake_request
    await client.song_url("186016", "standard")

    assert captured["path"] == "/song/url/v1"
    assert "timestamp" in captured["params"], "song_url 必须拼 timestamp 穿透 URL 缓存"
    assert captured["params"]["id"] == "186016"


async def test_ncm_song_mv_url_without_mvid():
    from astrbot_plugin_music_hub.core.api.ncm import NeteaseClient

    client = NeteaseClient.__new__(NeteaseClient)
    assert await client.song_mv_url({"name": "x"}) == {"url": ""}


# ── 酷狗登录守卫（回归 P1-5：匿名搜索必现 152）──


async def test_kg_search_requires_login():
    """酷狗已禁止匿名搜索：未登录时应前置拦截，不发无谓请求。"""
    from astrbot_plugin_music_hub.core.api.kg import KugouClient
    from astrbot_plugin_music_hub.core.errors import ApiError

    client = KugouClient.__new__(KugouClient)
    client._config = Config({"kg": {"apiBase": "http://x:4000", "cookie": ""}})

    called = []

    async def never(*_a, **_kw):
        called.append(1)
        return {}

    client.ensure_device = never
    client.request = never

    with pytest.raises(ApiError, match="登录"):
        await client.search("晴天")
    assert not called, "未登录时不应向上游发请求"


async def test_kg_search_allowed_after_login():
    from astrbot_plugin_music_hub.core.api.kg import KugouClient

    client = KugouClient.__new__(KugouClient)
    client._config = Config({"kg": {"apiBase": "http://x:4000", "cookie": "token=t;userid=1"}})

    async def fake_device():
        return "dfid=1"

    async def fake_request(path, params=None, **kw):
        return {"data": {"lists": []}}

    client.ensure_device = fake_device
    client.request = fake_request
    assert await client.search("晴天", limit=5) == []


def test_kg_request_has_no_anon_placeholder():
    """占位 cookie（token=kg;userid=1）已确认无效，_compose_cookie 不应再产出它。"""
    from astrbot_plugin_music_hub.core.api.kg import KugouClient

    client = KugouClient.__new__(KugouClient)
    client._device_cookie = ""
    client._config = Config({"kg": {"apiBase": "http://x:4000", "cookie": ""}})
    assert "token=kg" not in client._compose_cookie("")


# ── 适配器契约（core/api/base.py + registry.py）──
def test_all_clients_satisfy_source_contract():
    """三平台客户端必须完整实现 SourceClient。

    契约不全的后果是「用户点歌时才 AttributeError」，所以这里逐方法验。
    """
    from astrbot_plugin_music_hub.core.api.base import missing_methods
    from astrbot_plugin_music_hub.core.api.kg import KugouClient
    from astrbot_plugin_music_hub.core.api.ncm import NeteaseClient
    from astrbot_plugin_music_hub.core.api.qq import QQClient, available
    from astrbot_plugin_music_hub.core.config import Config

    cfg = Config({})
    clients = [NeteaseClient(cfg), KugouClient(cfg, None)]
    if available():
        clients.append(QQClient(cfg, None, None))
    for c in clients:
        missing = missing_methods(c)
        assert not missing, f"{c.source} 缺少协议方法：{missing}（用户点歌时才会炸）"
        assert c.ready() in (True, False)


def test_song_detail_is_platform_extra_not_core_contract():
    """song_detail 三家签名真的不同（ncm 收列表/qq 收标量/kg 没有），
    不该塞进统一契约里逼出无意义适配；它属于平台私有能力。"""
    from astrbot_plugin_music_hub.core.api.base import _TRACK_METHODS

    assert "song_detail" not in _TRACK_METHODS, (
        "song_detail 签名三平台不一致，应走 PlatformExtras 按 source 分派"
    )


def test_registry_exposes_all_three_sources():
    from astrbot_plugin_music_hub.core.api import registry

    assert set(registry.registered()) == {"ncm", "kg", "qq"}


def test_registry_create_returns_all_clients(tmp_path):
    """注册表实例化：新增平台只改 register()，service 不必改。"""
    from astrbot_plugin_music_hub.core.api import registry
    from astrbot_plugin_music_hub.core.config import Config

    clients = registry.create(Config({}), device_path=tmp_path / "dc.json", cred_path=tmp_path / "qc.json")
    assert set(clients) == set(registry.registered())


def test_registry_rejects_duplicate_registration():
    from astrbot_plugin_music_hub.core.api import registry

    with pytest.raises(RuntimeError, match="重复注册"):
        registry.register("ncm", lambda cfg, **kw: None)
