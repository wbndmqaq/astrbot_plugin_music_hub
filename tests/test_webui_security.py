"""WebUI 安全回归测试：Origin 校验、Host 白名单、登录流程与限速状态机。

这几处都是「看起来在防护、实际能被绕过」的类型，修复后必须有测试锁住，
否则后续重构很容易改回只比 hostname 的写法。
"""

from __future__ import annotations

import pytest
from astrbot_plugin_music_hub.core.resolve import is_allowed_redirect
from astrbot_plugin_music_hub.core.webui._server import WebUIServer


def _origin_ok(origin: str, host: str) -> bool:
    """直接调被测方法：它只用到 self 上的一次 DNS 校验，这里无需完整实例。"""
    return WebUIServer._origin_ok(None, origin, host)  # type: ignore[arg-type]


class TestOriginCheck:
    """Origin 必须与 Host 的 scheme+hostname+port 三者全等。"""

    def test_exact_match_passes(self):
        assert _origin_ok("http://127.0.0.1:17818", "127.0.0.1:17818") is True

    def test_default_port_matches_explicit(self):
        # Host 不带端口（80）+ Origin 带默认端口 → 同源
        assert _origin_ok("http://music.example.com", "music.example.com") is True

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:3000",  # 换端口（SameSite 视为同站点，最危险的绕过）
            "http://127.0.0.1:8080",
            "http://localhost:17818",  # 换主机名
            "https://127.0.0.1:17818",  # 换 scheme
        ],
    )
    def test_port_host_scheme_mismatch_rejected(self, origin):
        assert _origin_ok(origin, "127.0.0.1:17818") is False

    def test_ipv6_literal(self):
        """修复前 split(':')[0] 会把 [::1] 截成 '['，导致 IPv6 用户写操作全废。"""
        assert _origin_ok("http://[::1]:17818", "[::1]:17818") is True
        assert _origin_ok("http://[::1]:3000", "[::1]:17818") is False

    def test_empty_and_malformed_rejected(self):
        assert _origin_ok("", "127.0.0.1:17818") is False
        assert _origin_ok("not-a-url", "127.0.0.1:17818") is False


class TestRedirectAllowlist:
    """302 白名单必须逐项带前导点，否则裸后缀会被 evilurl.cn 命中。"""

    @pytest.mark.parametrize(
        "url",
        [
            "https://music.163.com/song?id=1",
            "https://m7.music.126.net/song.mp3",
            "https://y.qq.com/n/ryqq/songDetail/xxx",
            "https://y.gtimg.cn/music/photo.jpg",
            "https://url.cn/abc123",  # 精确匹配无前导点也要放行
            "https://qpic.cn/cover.jpg",
        ],
    )
    def test_legitimate_hosts_allowed(self, url):
        assert is_allowed_redirect(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "https://evilurl.cn/x",  # 裸后缀 url.cn 的前缀攻击
            "https://attacker-url.cn/a",
            "https://myqpic.cn/x",  # 裸后缀 qpic.cn 的前缀攻击
            "https://evil.com/?x=.163.com",  # 白名单串在 query 里
            "https://not163.com.evil.io/x",
        ],
    )
    def test_lookalike_hosts_rejected(self, url):
        assert is_allowed_redirect(url) is False

    def test_empty_rejected(self):
        assert is_allowed_redirect("") is False
        assert is_allowed_redirect("   ") is False


class _HostCfg:
    def __init__(self, allowlist):
        self.webui_host_allowlist = allowlist


def _host_ok(host: str, allowlist=()) -> bool:
    """直接调被测方法：新版实现是纯白名单比对，无需实例。"""
    return WebUIServer._host_ok(type("S", (), {"config": _HostCfg(list(allowlist))})(), host)


class TestHostCheck:
    """Host 必须是字面 IP / localhost / hostAllowlist 白名单域名。

    旧实现按「域名解析到私网」放行：多 A 记录（公网+私网）可恒通过，
    而纯公网域名的合法访问反被 403 —— 反解式校验已删除，回归时此用例会拦住。
    """

    async def test_literal_ips_and_localhost_pass(self):
        assert await _host_ok("") is True
        assert await _host_ok("127.0.0.1:17818") is True
        assert await _host_ok("[::1]:17818") is True
        assert await _host_ok("localhost") is True
        assert await _host_ok("192.168.1.5:17818") is True
        assert await _host_ok("8.8.8.8") is True  # 字面 IP 没有 rebinding 向量

    async def test_domain_without_allowlist_rejected(self):
        assert await _host_ok("panel.example.com:17818") is False

    @pytest.mark.parametrize(
        "host",
        [
            "panel.example.com",
            "panel.example.com:17818",
            "a.panel.example.com",  # 子域名自动放行
            "PANEL.EXAMPLE.COM",  # 大小写不敏感
        ],
    )
    async def test_allowlisted_domain_passes(self, host):
        assert await _host_ok(host, ["panel.example.com"]) is True

    @pytest.mark.parametrize(
        "host",
        [
            "evilexample.com",  # 非子域
            "panel.example.com.evil.io",  # 后缀攻击
            "evil-panel.example.com",  # 前缀攻击
        ],
    )
    async def test_lookalike_domains_rejected(self, host):
        assert await _host_ok(host, ["panel.example.com"]) is False

    async def test_userinfo_trick_resolves_to_effective_host(self):
        """Host 带 @ 时按「@ 后是有效主机」解析（与 urlparse 一致）：
        真实主机是白名单外 → 拒；是白名单内 → 放。"""
        assert await _host_ok("panel.example.com@evil.com", ["panel.example.com"]) is False
        assert await _host_ok("evil.com@panel.example.com", ["panel.example.com"]) is True


# ── 登录流程集成：Cookie 属性与限速状态机 ──
@pytest.fixture
def auth(tmp_path):
    from astrbot_plugin_music_hub.core.webui._auth import AuthManager

    return AuthManager(tmp_path, lambda: "pw123456")


class TestLoginRateLimit:
    def test_blocks_after_max_fails(self, auth):
        from astrbot_plugin_music_hub.core.webui._auth import RATE_MAX_FAILS

        for _ in range(RATE_MAX_FAILS):
            auth.record_fail("1.2.3.4")
        assert auth.login_blocked("1.2.3.4") > 0

    def test_success_clears_fails(self, auth):
        auth.record_fail("1.2.3.4")
        auth.record_fail("1.2.3.4")
        auth.record_success("1.2.3.4")
        auth.record_fail("1.2.3.4")
        auth.record_fail("1.2.3.4")
        auth.record_fail("1.2.3.4")
        assert auth.login_blocked("1.2.3.4") == 0, "成功登录应清空失败计数"

    def test_window_expiry_unblocks(self, auth, monkeypatch):
        from astrbot_plugin_music_hub.core.webui import _auth as auth_mod

        for _ in range(auth_mod.RATE_MAX_FAILS):
            auth.record_fail("1.2.3.4")
        assert auth.login_blocked("1.2.3.4") > 0
        # 把封禁时间拨回窗口之外
        auth._blocked["1.2.3.4"] = 0.0
        assert auth.login_blocked("1.2.3.4") == 0

    def test_password_constant_time_path_handles_unicode(self, auth):
        assert auth.check_password("pw123456") is True
        assert auth.check_password("中文密码≠ASCII") is False  # 非 ASCII 不得抛 TypeError
        assert auth.check_password("") is False


class TestLoginHandler:
    """走真实 aiohttp 应用：钉住登录响应的 Cookie 属性与限速 429 行为。"""

    def _app(self, tmp_path):
        from aiohttp import web
        from astrbot_plugin_music_hub.core.webui._api_auth import make_login
        from astrbot_plugin_music_hub.core.webui._auth import AuthManager

        auth = AuthManager(tmp_path, lambda: "pw123456")
        app = web.Application()
        app.router.add_post("/login", make_login(type("S", (), {"auth": auth})()))
        return app, auth

    async def test_success_sets_hardened_cookie(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer

        app, _auth = self._app(tmp_path)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/login", json={"password": "pw123456"})
            assert resp.status == 200
            raw = resp.headers.get("Set-Cookie", "")
            assert "HttpOnly" in raw, "会话 cookie 必须 HttpOnly"
            assert "SameSite=Lax" in raw, "CSRF 防线之一是 SameSite=Lax"
            assert "Max-Age=" in raw, "cookie 必须带过期时间"
        finally:
            await client.close()

    async def test_rate_limit_returns_429_through_handler(self, tmp_path):
        from aiohttp.test_utils import TestClient, TestServer
        from astrbot_plugin_music_hub.core.webui._auth import RATE_MAX_FAILS

        app, _auth = self._app(tmp_path)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for _ in range(RATE_MAX_FAILS):
                resp = await client.post("/login", json={"password": "wrong"})
                assert resp.status == 401
            # 连续错满后，即使密码正确也拒绝：429 优先于密码校验
            resp = await client.post("/login", json={"password": "pw123456"})
            assert resp.status == 429
        finally:
            await client.close()
