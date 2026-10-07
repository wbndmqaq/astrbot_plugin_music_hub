"""AUDIT 2026-10-06 v3 · WebUI 后端与链接解析层修复的回归测试。

覆盖：空 host 写入映射回环地址、短链展开护栏（条数上限 / 文本窗口）、
defaultSource 值域白名单、identifyPrefix 长度与控制字符约束、clamp_int
严格化（拒绝 bool / OverflowError）、keepTotals 严格 bool、_guard 的空 Host
403 与 Sec-Fetch-Site 第三层 CSRF、body_json 的 413 透传与 client_max_size 声明。

v4 追加（12-16 节）：账号页对依赖缺失的行级降级、refresh 缺库 400、
acl_save 严格 list、config_save 记账（未知嵌套键 / 空节点 / qq.apiBase）、
resolve 502 契约。
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from astrbot_plugin_music_hub.core.config import Config
from astrbot_plugin_music_hub.core.webui._server import WebUIServer


# ──────────── 假件 ────────────
class _JsonReq:
    """body_json 只消费 request.json()，假请求足够触发真实解析路径。"""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


class _GuardReq:
    """_guard 的前置校验只读 method / path / headers。"""

    method = "POST"
    path = "/"

    def __init__(self, headers):
        self.headers = headers


async def _ok_handler(request):
    return web.Response(text="ok")


class _StatsStub:
    def __init__(self):
        self.calls = []

    def reset(self, keep_totals):
        self.calls.append(keep_totals)

    async def flush(self):
        return None


def _webui_stub() -> WebUIServer:
    # Host 前缀校验对字面 IP 不触达 config，__new__ 跳过 __init__ 的重依赖即可
    return WebUIServer.__new__(WebUIServer)


# ──────────── 1. 空 host 写入映射回环 ────────────
async def test_empty_host_save_maps_to_loopback():
    server = type("S", (), {"config": Config({"webui": {"host": "", "port": 17818}})})()
    resp = await _config_save(server)(_JsonReq({"webui": {"host": "   "}}))
    body = json.loads(resp.text)
    assert "webui" in body["applied"]
    # 断言落盘值而非读取侧回落值：读取侧本来就会把空值兜成 127.0.0.1，
    # 只有查原始节点才能证明写入侧不再存 0.0.0.0
    assert server.config.get("webui")["host"] == "127.0.0.1"
    assert server.config.webui_host == "127.0.0.1"


async def test_explicit_bind_host_is_preserved():
    """只纠正空值兜底：用户显式写的 0.0.0.0 仍按本人意愿保留。"""
    server = type("S", (), {"config": Config({"webui": {}})})()
    resp = await _config_save(server)(_JsonReq({"webui": {"host": "0.0.0.0"}}))
    assert "webui" in json.loads(resp.text)["applied"]
    assert server.config.get("webui")["host"] == "0.0.0.0"


def _config_save(server):
    from astrbot_plugin_music_hub.core.webui._api_admin import make_config_save

    return make_config_save(server)


# ──────────── 2. 短链展开护栏 ────────────
class _FakeResp:
    status = 302
    headers = {"Location": "https://music.163.com/song?id=1"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, calls):
        self._calls = calls

    def get(self, url, **kwargs):
        self._calls.append(url)
        return _FakeResp()


async def test_expand_short_links_capped(monkeypatch):
    from astrbot_plugin_music_hub.core import resolve

    calls: list[str] = []
    monkeypatch.setattr(resolve, "get_session", lambda: _FakeSession(calls))
    text = " ".join(f"https://163cn.tv/a{i}" for i in range(8))
    out = await resolve.expand_short_links(text)
    assert len(calls) == resolve._EXPAND_MAX, "每请求出站 GET 必须有上限"
    assert out.count("https://music.163.com/song?id=1") == resolve._EXPAND_MAX
    assert "https://163cn.tv/a7" in out, "超出上限的短链必须原样保留"


async def test_expand_short_links_ignores_beyond_text_window(monkeypatch):
    from astrbot_plugin_music_hub.core import resolve

    calls: list[str] = []
    monkeypatch.setattr(resolve, "get_session", lambda: _FakeSession(calls))
    text = "-" * resolve._EXPAND_TEXT_MAX + " https://163cn.tv/tail"
    out = await resolve.expand_short_links(text)
    assert calls == [], "匹配窗口外的短链不得触发出站请求"
    assert out == text


# ──────────── 4. defaultSource 白名单 / identifyPrefix 约束 ────────────
async def test_default_source_outside_whitelist_rejected():
    server = type("S", (), {"config": Config({"defaultSource": "auto"})})()
    resp = await _config_save(server)(_JsonReq({"defaultSource": "spotify"}))
    body = json.loads(resp.text)
    assert body["rejected"] == ["defaultSource"]
    assert server.config.get("defaultSource") == "auto", "白名单外的值不得写入"
    # 合法值（含空白）仍可写
    resp2 = await _config_save(server)(_JsonReq({"defaultSource": " ncm "}))
    assert "defaultSource" in json.loads(resp2.text)["applied"]
    assert server.config.default_source == "ncm"


async def test_identify_prefix_guarded():
    server = type("S", (), {"config": Config({})})()
    # 校验发生在 strip 之后：首尾空白会被正常清掉，只有残留内部的换行/控制字符才违规
    for bad in ("识别：\n尾", "识\t别：", "x" * 21):
        resp = await _config_save(server)(_JsonReq({"identifyPrefix": bad}))
        assert json.loads(resp.text)["rejected"] == ["identifyPrefix"], repr(bad)
    # 合法前缀照常写入（strip 后落盘）
    resp = await _config_save(server)(_JsonReq({"identifyPrefix": "  识别：  "}))
    assert "identifyPrefix" in json.loads(resp.text)["applied"]
    assert server.config.get("identifyPrefix") == "识别："


# ──────────── 3. clamp_int 严格化 ────────────
def test_clamp_int_rejects_bool():
    from astrbot_plugin_music_hub.core.webui._api_admin import clamp_int

    with pytest.raises(TypeError):
        clamp_int("maxList", True)
    with pytest.raises(TypeError):
        clamp_int("maxList", False)


def test_clamp_int_overflow_becomes_rejected_value_error():
    """JSON 1e999 解析成 float('inf')：必须归入 rejected，不得以 OverflowError 穿透成 500。"""
    from astrbot_plugin_music_hub.core.webui._api_admin import clamp_int

    with pytest.raises((TypeError, ValueError)):
        clamp_int("maxList", 1e999)


# ──────────── 5. keepTotals 严格 bool ────────────
async def test_stats_reset_rejects_non_bool_keep_totals():
    from astrbot_plugin_music_hub.core.webui._api_admin import make_stats_reset

    stats = _StatsStub()
    server = type("S", (), {"service": type("Svc", (), {"stats": stats})()})()
    handler = make_stats_reset(server)
    # "false" 宽松强转会变 True：必须拒绝而不是反向清空总计
    resp = await handler(_JsonReq({"keepTotals": "false"}))
    assert resp.status == 400
    assert stats.calls == []
    resp = await handler(_JsonReq({}))
    assert resp.status == 200
    assert stats.calls == [True]
    resp = await handler(_JsonReq({"keepTotals": False}))
    assert resp.status == 200
    assert stats.calls == [True, False]


# ──────────── 6/7. _guard：空 Host 403 与 Sec-Fetch-Site 第三层 ────────────
async def test_guard_rejects_empty_host():
    resp = await _webui_stub()._guard(_GuardReq({}), _ok_handler)
    assert resp.status == 403


async def test_guard_rejects_cross_site_fetch_metadata():
    req = _GuardReq({"Host": "127.0.0.1:17818", "Sec-Fetch-Site": "cross-site"})
    resp = await _webui_stub()._guard(req, _ok_handler)
    assert resp.status == 403


async def test_guard_accepts_same_origin_and_absent_fetch_metadata():
    """same-origin 放行；缺席（老客户端/非浏览器工具）也放行走既有兜底。"""
    server = _webui_stub()
    for headers in (
        {"Host": "127.0.0.1:17818", "Sec-Fetch-Site": "same-origin"},
        {"Host": "127.0.0.1:17818"},
    ):
        resp = await server._guard(_GuardReq(headers), _ok_handler)
        assert resp.status == 200, headers


# ──────────── 9. body_json 的 413 透传与 client_max_size 声明 ────────────
async def test_body_json_reraises_payload_too_large():
    """修复前 413 被吞成空 dict，登录接口会误报「密码错误」。"""
    from astrbot_plugin_music_hub.core.webui._util import body_json

    async def handler(request):
        await body_json(request)
        return web.Response(text="ok")

    app = web.Application(client_max_size=16)
    app.router.add_post("/", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.post("/", json={"k": "x" * 64})
        assert resp.status == 413
    finally:
        await client.close()


def test_app_declares_client_max_size():
    server = _webui_stub()
    app = server._build_app()
    assert app._client_max_size == 1024**2, "1MB 上限必须显式声明而非依赖 aiohttp 默认值"


# ──────────── 12. 账号页对依赖缺失的行级降级（gather 参数求值顺序坑） ────────────
class _AcctCfg:
    """make_accounts 只读 src_enabled / src_quality。"""

    def src_enabled(self, src):
        return True

    def src_quality(self, src):
        return "auto"


class _FakeNcmClient:
    async def login_status(self):
        return {"loggedIn": True, "nickname": "测试用户", "uid": "u1"}


class _AcctService:
    """ncm 正常、kg/qq 依赖缺失（client_of 抛 NotEnabledError）的现场。"""

    def __init__(self):
        self._clients = {"ncm": _FakeNcmClient()}

    def client_of(self, source):
        from astrbot_plugin_music_hub.core.errors import NotEnabledError

        client = self._clients.get(source)
        if client is None:
            raise NotEnabledError("音源不可用（未安装依赖或初始化失败）", source=source)
        return client

    async def vip_summary(self, source):
        return ""

    async def grade_summary(self, source):
        return ""


async def test_accounts_degrades_row_when_client_missing():
    """修复前 `service.client_of(src).login_status()` 在 gather 参数构造时同步求值，
    NotEnabledError 发生在 _safe 的 try 之外，一行炸掉整页（500）。"""
    from astrbot_plugin_music_hub.core.webui._api_music import make_accounts

    server = type("S", (), {"config": _AcctCfg(), "service": _AcctService()})()
    resp = await make_accounts(server)(None)
    assert resp.status == 200
    rows = {r["source"]: r for r in json.loads(resp.text)["accounts"]}
    assert set(rows) == {"ncm", "kg", "qq"}
    # 正常音源照常回填；缺失音源只降级该行，不拖垮其它行
    assert rows["ncm"]["loggedIn"] is True
    assert rows["ncm"]["nickname"] == "测试用户"
    assert rows["kg"]["loggedIn"] is False
    assert rows["kg"]["nickname"] == "查询失败"
    assert rows["qq"]["loggedIn"] is False
    assert rows["qq"]["nickname"] == "查询失败"


# ──────────── 13. /api/accounts/refresh 在 QQ 库缺失时 400（原为 AttributeError → 500） ────────────
async def test_accounts_refresh_returns_400_when_qq_missing():
    from astrbot_plugin_music_hub.core.webui._api_music import make_account_refresh

    server = type("S", (), {"service": type("Svc", (), {"qq": None})()})()
    resp = await make_account_refresh(server)(_JsonReq({"source": "qq"}))
    assert resp.status == 400
    assert "qqmusic-api-python" in json.loads(resp.text)["error"]


async def test_accounts_refresh_ok_when_qq_present():
    from astrbot_plugin_music_hub.core.webui._api_music import make_account_refresh

    class _QQ:
        async def refresh_credential(self):
            return True

    server = type("S", (), {"service": type("Svc", (), {"qq": _QQ()})()})()
    resp = await make_account_refresh(server)(_JsonReq({"source": "qq"}))
    assert json.loads(resp.text) == {"ok": True, "msg": "刷新成功"}


# ──────────── 14. acl_save 严格 list：非 list 400，不再静默清空名单 ────────────
def _acl_server():
    return type("S", (), {"config": Config({"acl": {"mode": "off", "blacklist": ["10001"]}})})()


async def test_acl_save_rejects_non_list():
    from astrbot_plugin_music_hub.core.webui._api_admin import make_acl_save

    server = _acl_server()
    for payload in ({"blacklist": "123456"}, {"blacklist": 123}, {"whitelist": {"a": 1}}):
        resp = await make_acl_save(server)(_JsonReq(payload))
        assert resp.status == 400, payload
        assert "必须是字符串数组" in json.loads(resp.text)["error"]
    # 被拒的请求不得触碰已有名单
    assert server.config.acl_list("blacklist") == ["10001"]


async def test_acl_save_list_and_null_semantics():
    from astrbot_plugin_music_hub.core.webui._api_admin import make_acl_save

    server = _acl_server()
    # list 照常清洗（strip / 去重 / 丢空）
    resp = await make_acl_save(server)(_JsonReq({"blacklist": [" 20002 ", "20002", ""]}))
    body = json.loads(resp.text)
    assert body["ok"] is True
    assert server.config.acl_list("blacklist") == ["20002"]
    # null = 不改动该项（set_acl 的 None 语义），mode 独立生效
    resp = await make_acl_save(server)(_JsonReq({"whitelist": None, "mode": "blacklist"}))
    assert json.loads(resp.text)["ok"] is True
    assert server.config.acl_mode == "blacklist"
    assert server.config.acl_list("blacklist") == ["20002"]


# ──────────── 15. config_save 记账：未知嵌套键、空节点、qq.apiBase ────────────
async def test_config_save_node_mixed_keys_accounted():
    server = type("S", (), {"config": Config({})})()
    resp = await _config_save(server)(_JsonReq({"ncm": {"quality": "lossless", "foo": 1}}))
    body = json.loads(resp.text)
    assert "ncm.quality" in body["applied"]
    assert "ncm.foo" in body["rejected"], "嵌套未知键必须进 rejected，不能静默忽略"
    assert body["ok"] is True


async def test_config_save_all_rejected_node_writes_nothing():
    server = type("S", (), {"config": Config({"ncm": {"quality": "auto"}})})()
    resp = await _config_save(server)(_JsonReq({"ncm": {"foo": 1}}))
    body = json.loads(resp.text)
    assert body["applied"] == []
    assert body["rejected"] == ["ncm.foo"]
    assert body["ok"] is False, "一个字段都没落盘时不得报 ok"


async def test_config_save_qq_apibase_rejected():
    """QQ 走内置库直连，apiBase 无消费者且读取侧不回显：写侧按未知键拒绝。"""
    server = type("S", (), {"config": Config({})})()
    resp = await _config_save(server)(_JsonReq({"qq": {"apiBase": "http://x", "quality": "320"}}))
    body = json.loads(resp.text)
    assert "qq.apiBase" in body["rejected"]
    assert "qq.quality" in body["applied"]


async def test_config_save_empty_node_is_not_ok():
    """空节点 {} 原样回写曾把「空保存」标成 ok。"""
    server = type("S", (), {"config": Config({})})()
    resp = await _config_save(server)(_JsonReq({"webui": {}, "scheduler": {}}))
    body = json.loads(resp.text)
    assert body["applied"] == []
    assert body["ok"] is False


# ──────────── 16. /api/resolve 对齐全插件非 2xx 契约 ────────────
async def test_resolve_upstream_failure_returns_502():
    """修复前错误混在 200 响应体里（全插件唯一的非 2xx 契约例外）。"""
    from astrbot_plugin_music_hub.core.webui._api_music import make_resolve

    class _Ncm:
        async def song_detail(self, ids):
            raise RuntimeError("boom")

    server = type("S", (), {"service": type("Svc", (), {"ncm": _Ncm()})()})()
    resp = await make_resolve(server)(_JsonReq({"text": "https://music.163.com/song?id=1"}))
    assert resp.status == 502
    body = json.loads(resp.text)
    assert body["error"] == "解析失败，请稍后重试"
    assert body["matched"] == ["ncm"]
    # expanded 只回显 host 部分（query 里的签名 token 不回传浏览器）
    assert body["expanded"] == "https://music.163.com/song"


async def test_resolve_unknown_source_returns_400():
    from astrbot_plugin_music_hub.core.webui._api_music import make_account_cookie

    server = type("S", (), {"config": Config({})})()
    resp = await make_account_cookie(server)(_JsonReq({"source": "spotify", "value": "x"}))
    assert resp.status == 400
