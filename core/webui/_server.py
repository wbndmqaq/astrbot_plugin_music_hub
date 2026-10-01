"""WebUI aiohttp 服务器：应用构建、路由、静态资源（CSP）、启停。"""

from __future__ import annotations

import asyncio
import ipaddress
import traceback
from collections import OrderedDict
from pathlib import Path

from aiohttp import web

from .. import DISPLAY_NAME
from . import _api
from ._auth import COOKIE_NAME, AuthManager
from ._util import json_response as _json

SHUTDOWN_TIMEOUT = 2

PUBLIC_PATHS = {
    "/",
    "/webui/style.css",
    "/webui/app.js",
    "/webui/theme-boot.js",
    "/webui/logo.svg",
    "/api/meta",
    "/api/auth/login",
    "/api/auth/check",
}

# 安全响应头：严格 CSP，脚本只允许本站
# media-src 放开 http:：歌曲 CDN 直链多为 http，且面板本身是 http 服务无混合内容问题
CSP = (
    "default-src 'self'; "
    "img-src 'self' https: data:; "
    "media-src 'self' http: https:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class WebUIServer:
    def __init__(self, service):
        self.service = service
        self.config = service.config
        self.log = service.log
        self.dir = Path(__file__).resolve().parent.parent.parent / "webui"
        self.auth = AuthManager(service.data_dir, lambda: self.config.webui_password)
        self._runner: web.AppRunner | None = None
        self.host = self.config.webui_host
        self.port = self.config.webui_port
        # 搜索结果歌曲缓存（source:sid → 完整归一化歌曲）：试听 / 远程投递取流用
        self._song_cache: OrderedDict[str, dict] = OrderedDict()

    # ──────────── 歌曲缓存 ────────────
    def cache_song(self, song: dict) -> None:
        sid = str(song.get("sid") or "")
        if not sid:
            return
        key = f"{song.get('source', '')}:{sid}"
        self._song_cache[key] = song
        self._song_cache.move_to_end(key)
        while len(self._song_cache) > 400:
            self._song_cache.popitem(last=False)

    def get_song(self, source: str, sid: str) -> dict | None:
        return self._song_cache.get(f"{source}:{sid}")

    # ──────────── 中间件 ────────────
    async def _guard(self, request: web.Request, handler):
        # Host 校验（防 DNS rebinding：公网域名解析到非私网地址一律拒绝）
        if not await self._host_ok(request.headers.get("Host", "")):
            return _json({"error": "非法 Host"}, 403)
        # 非 GET 的 Origin 同源校验（防 CSRF）
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin", "")
            if origin:
                if not self._origin_ok(origin, request.headers.get("Host", "")):
                    return _json({"error": "跨站请求被拒绝"}, 403)
        # 鉴权（default-deny）
        if request.path not in PUBLIC_PATHS and not self._authed(request):
            return _json({"error": "未登录或会话已过期"}, 401)
        return await handler(request)

    async def _host_ok(self, host: str) -> bool:
        if not host:
            return True
        h = host.strip()
        if "@" in h:
            h = h.rsplit("@", 1)[1]
        if h.startswith("["):  # IPv6 字面量（[::1]:17818）
            netloc = h[1 : h.find("]")].lower()
        else:
            netloc = h.split(":", 1)[0].lower()
        if not netloc:
            return True
        if netloc in ("localhost",):
            return True
        try:
            ipaddress.ip_address(netloc)
            return True  # 字面 IP（私网/公网/回环）没有 DNS rebinding 向量，放行
        except ValueError:
            pass
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(netloc, None)
        except Exception:  # noqa: BLE001
            return False  # 域名解析失败时无法证明其指向本机，拒绝（DNS rebinding 不给放行窗口）
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if ip.is_loopback or ip.is_private or ip.is_link_local:
                return True
        # 注：开启 TUN/fake-IP 代理的本机（伪 DNS 解析到 198.18.0.0/15）会被
        # ipaddress 视为私有网段而放行 —— 该场景下面板以登录口令为主要防线。
        return False  # 域名只解析到公网地址 → 拒绝

    def _origin_ok(self, origin: str, host: str) -> bool:
        try:
            from urllib.parse import urlparse

            o = urlparse(origin)
            o_host = (o.hostname or "").lower()
            h_host = (host or "").split(":")[0].lower()
            return bool(o_host) and o_host == h_host
        except Exception:  # noqa: BLE001
            return False

    def _authed(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE_NAME, "")
        payload = self.auth.verify(token)
        if payload:
            request["jti"] = payload.get("jti", "")
            request["user"] = payload.get("sub", "")
            return True
        # 也接受 Authorization: Bearer（SSE/下载便利）
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            payload = self.auth.verify(auth_header[7:])
            if payload:
                request["jti"] = payload.get("jti", "")
                return True
        return False

    async def _handle_errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            self.log.error(f"[music_hub] WebUI 请求异常：\n{tb}")
            return _json({"error": "服务器内部错误"}, 500)

    # ──────────── 应用 ────────────
    def _build_app(self) -> web.Application:
        @web.middleware
        async def guard_mw(request, handler):
            return await self._guard(request, handler)

        @web.middleware
        async def errors_mw(request, handler):
            return await self._handle_errors(request, handler)

        app = web.Application(middlewares=[errors_mw, guard_mw])
        app["server"] = self
        r = app.router
        # 静态
        r.add_get("/", self._index)
        r.add_get("/webui/style.css", self._static("style.css", "text/css"))
        r.add_get("/webui/app.js", self._static("app.js", "application/javascript"))
        r.add_get("/webui/theme-boot.js", self._static("theme-boot.js", "application/javascript"))
        r.add_get("/webui/logo.svg", self._static("logo.svg", "image/svg+xml"))
        # 元信息 / 认证
        r.add_get("/api/meta", self._meta)
        r.add_post("/api/auth/login", _api.make_login(self))
        r.add_post("/api/auth/logout", _api.make_logout(self))
        r.add_get("/api/auth/check", _api.make_check(self))
        r.add_post("/api/auth/change-password", _api.make_change_password(self))
        r.add_get("/api/auth/sessions", _api.make_sessions(self))
        r.add_post("/api/auth/sessions/revoke", _api.make_revoke(self))
        # 总览 / 统计
        r.add_get("/api/overview", _api.make_overview(self))
        r.add_get("/api/stats", _api.make_stats(self))
        r.add_post("/api/stats/reset", _api.make_stats_reset(self))
        # 配置
        r.add_get("/api/config", _api.make_config_get(self))
        r.add_post("/api/config/save", _api.make_config_save(self))
        # 黑白名单
        r.add_get("/api/acl", _api.make_acl_get(self))
        r.add_post("/api/acl/save", _api.make_acl_save(self))
        # 账号
        r.add_get("/api/accounts", _api.make_accounts(self))
        r.add_post("/api/accounts/logout", _api.make_account_logout(self))
        r.add_post("/api/accounts/refresh", _api.make_account_refresh(self))
        r.add_post("/api/accounts/cookie", _api.make_account_cookie(self))
        # 扫码登录
        r.add_post("/api/qr/start", _api.make_qr_start(self))
        r.add_get("/api/qr/status", _api.make_qr_status(self))
        r.add_post("/api/qr/cancel", _api.make_qr_cancel(self))
        # 服务检查
        r.add_get("/api/service/check", _api.make_service_check(self))
        # 多音源搜索 / 链接解析工具
        r.add_get("/api/search", _api.make_search(self))
        r.add_post("/api/resolve", _api.make_resolve(self))
        # 试听 / 远程投递 / 播放历史 / 运行日志
        r.add_get("/api/preview", _api.make_preview(self))
        r.add_get("/api/remote/scopes", _api.make_remote_scopes(self))
        r.add_post("/api/remote/play", _api.make_remote_play(self))
        r.add_get("/api/history", _api.make_history(self))
        r.add_get("/api/logs", _api.make_logs(self))
        return app

    async def start(self) -> None:
        if self._runner is not None:
            return
        app = self._build_app()
        self._runner = web.AppRunner(
            app, access_log=None, shutdown_timeout=SHUTDOWN_TIMEOUT, keepalive_timeout=15
        )
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        try:
            await site.start()
        except OSError as e:
            await self._runner.cleanup()
            self._runner = None
            raise RuntimeError(f"WebUI 端口 {self.port} 启动失败：{e}") from e
        self.log.info(f"[music_hub] WebUI 已启动：http://{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._runner:
            runner, self._runner = self._runner, None
            try:
                await asyncio.wait_for(runner.cleanup(), timeout=5)
            except (TimeoutError, Exception):  # noqa: BLE001
                pass

    # ──────────── 静态 ────────────
    async def _index(self, request: web.Request) -> web.Response:
        return await self._file("index.html", "text/html")

    def _static(self, fname: str, ctype: str):
        async def _handler(request: web.Request) -> web.Response:
            return await self._file(fname, ctype)

        return _handler

    async def _file(self, fname: str, ctype: str) -> web.Response:
        try:
            body = await asyncio.to_thread((self.dir / fname).read_bytes)
            return web.Response(
                body=body,
                content_type=ctype,
                charset="utf-8",
                headers={
                    "Cache-Control": "no-store",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": CSP,
                },
            )
        except OSError:
            return _json({"error": "面板资源缺失，请检查插件安装完整性"}, 500)

    async def _meta(self, request: web.Request) -> web.Response:
        return _json(
            {
                "name": DISPLAY_NAME,
                "plugin": "astrbot_plugin_music_hub",
                "version": self._version(),
                "authed": self._authed(request),
            }
        )

    @staticmethod
    def _version() -> str:
        try:
            from ..help_data import VERSION

            return VERSION
        except Exception:  # noqa: BLE001
            return ""
