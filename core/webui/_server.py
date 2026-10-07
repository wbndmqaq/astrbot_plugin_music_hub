"""WebUI aiohttp 服务器：应用构建、路由、静态资源（CSP）、启停。"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import time
import traceback
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web

from .. import DISPLAY_NAME
from . import _api
from ._auth import COOKIE_NAME, AuthManager
from ._util import json_response as _json

# 搜索结果缓存：容量与有效期都要有上限，否则 WebUI 面板长期开着会累积陈旧对象
SONG_CACHE_MAX = 400
SONG_CACHE_TTL = 900.0

SHUTDOWN_TIMEOUT = 2

PUBLIC_PATHS = {
    "/",
    "/webui/style.css",
    "/webui/app.js",
    "/webui/theme-boot.js",
    "/webui/logo.png",
    "/webui/logos/ncm.svg",
    "/webui/logos/ncm.png",
    "/webui/logos/kg.png",
    "/webui/logos/qq.png",
    "/api/meta",
    "/api/auth/login",
    "/api/auth/check",
}

# 安全响应头：严格 CSP，脚本只允许本站
# media-src 放开 http:：歌曲 CDN 直链多为 http，且面板本身是 http 服务无混合内容问题
CSP = (
    "default-src 'self'; "
    # img-src 含 http:：不少音乐 CDN 封面只有 http 直链，面板本身不走 https 无混内容问题
    "img-src 'self' http: https: data:; "
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
        self._song_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        # 静态资源缓存：fname → (mtime, body, etag)
        self._file_cache: dict[str, tuple[float, bytes, str]] = {}

    # ──────────── 歌曲缓存 ────────────
    def cache_song(self, song: dict) -> None:
        sid = str(song.get("sid") or "")
        if not sid:
            return
        key = f"{song.get('source', '')}:{sid}"
        self._song_cache[key] = (time.time(), song)
        self._song_cache.move_to_end(key)
        while len(self._song_cache) > SONG_CACHE_MAX:
            self._song_cache.popitem(last=False)

    def get_song(self, source: str, sid: str) -> dict | None:
        # 必须判过期：缓存里的 song 带 cover 等字段，长期不过期会让远程投递
        # 拿到陈旧的封面与元数据，也让 sid 复用时串到旧对象
        hit = self._song_cache.get(f"{source}:{sid}")
        if hit is None:
            return None
        ts, song = hit
        if time.time() - ts > SONG_CACHE_TTL:
            self._song_cache.pop(f"{source}:{sid}", None)
            return None
        return song

    # ──────────── 中间件 ────────────
    async def _guard(self, request: web.Request, handler):
        # Host 校验（防 DNS rebinding：非 IP / localhost / 白名单的域名一律拒绝）
        if not await self._host_ok(request.headers.get("Host", "")):
            return _json({"error": "非法 Host：用域名访问面板需在插件配置 webui.hostAllowlist 登记"}, 403)
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
        """Host 必须是字面 IP、localhost 或 webui.hostAllowlist 里的域名。

        旧实现按「域名是否解析到私网地址」放行，两个方向都失效：攻击者域名同时配
        公网 + 私网两条 A 记录即可恒通过（rebinding 挡不住）；面板部署在 VPS 用域名
        访问时，域名只解析到公网反而每个请求都被 403。反解式校验已弃用 —— CSRF 另有
        Origin 同源 + SameSite cookie 双层防线，这里只做严格白名单。
        """
        if not host:
            return True
        h = host.strip()
        if "@" in h:
            h = h.rsplit("@", 1)[1]
        if h.startswith("["):  # IPv6 字面量（[::1]:17818）
            netloc = h[1 : h.find("]")].lower()
        else:
            netloc = h.split(":", 1)[0].lower()
        if not netloc or netloc == "localhost":
            return True
        try:
            ipaddress.ip_address(netloc)
            return True  # 字面 IP（私网/公网/回环）没有 DNS rebinding 向量，放行
        except ValueError:
            pass
        allowed = self.config.webui_host_allowlist
        return any(netloc == d or netloc.endswith("." + d) for d in allowed)

    def _origin_ok(self, origin: str, host: str) -> bool:
        """Origin 必须与 Host 完全一致：scheme + hostname + port 三者全等。

        只比 hostname 会被端口差异绕过 —— SameSite 的同站点判定同样忽略端口，
        `localhost:3000` 与 `localhost:17818` 互为同站点，Lax cookie 照常携带，
        本机任意 web 服务都能发起带凭据的写请求（改 apiBase 即等于外泄平台 cookie）。
        用 urlparse 而非 split(":"):0 以正确剥离 IPv6 方括号。
        """
        try:
            o = urlparse(origin)
            h = urlparse(f"http://{host or ''}")
            if not o.hostname or not h.hostname:
                return False
            if o.scheme != h.scheme or o.hostname.lower() != h.hostname.lower():
                return False
            # 无显式端口时按 scheme 取默认端口，http/https 混用也要挡住
            o_port = o.port or (443 if o.scheme == "https" else 80)
            h_port = h.port or (443 if h.scheme == "https" else 80)
            return o_port == h_port
        except ValueError:
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

        @web.middleware
        async def security_headers_mw(request, handler):
            # 统一注入：原先安全头只挂在 _file 上，所有 /api/* JSON 响应都没有，
            # 新增路由很容易漏挂
            resp = await handler(request)
            resp.headers.setdefault("X-Content-Type-Options", "nosniff")
            resp.headers.setdefault("X-Frame-Options", "DENY")
            resp.headers.setdefault("Referrer-Policy", "no-referrer")
            return resp

        app = web.Application(middlewares=[security_headers_mw, errors_mw, guard_mw])
        app["server"] = self
        r = app.router
        # 静态
        r.add_get("/", self._index)
        r.add_get("/webui/style.css", self._static("style.css", "text/css"))
        r.add_get("/webui/app.js", self._static("app.js", "application/javascript"))
        r.add_get("/webui/theme-boot.js", self._static("theme-boot.js", "application/javascript"))
        r.add_get("/webui/logo.png", self._static("logo.png", "image/png"))
        # 三平台官方标识（侧边栏 / 音源标签用）
        r.add_get("/webui/logos/ncm.svg", self._static("logos/ncm.svg", "image/svg+xml"))
        for _name in ("ncm", "kg", "qq"):
            r.add_get(f"/webui/logos/{_name}.png", self._static(f"logos/{_name}.png", "image/png"))
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
            except Exception as e:  # noqa: BLE001
                # 静默吞掉会让「端口没释放」这类问题彻底无线索，
                # 下次重载表现为 OSError: 地址已使用
                self.log.warning(f"[music_hub] WebUI 关闭异常（端口可能未释放）：{e}")

    # ──────────── 静态 ────────────
    async def _index(self, request: web.Request) -> web.Response:
        return await self._file("index.html", "text/html", request)

    def _static(self, fname: str, ctype: str):
        async def _handler(request: web.Request) -> web.Response:
            return await self._file(fname, ctype, request)

        return _handler

    async def _file(self, fname: str, ctype: str, request: web.Request | None = None) -> web.Response:
        """静态资源。按 mtime 缓存内容 + ETag：40KB CSS + 55KB JS 每次整页加载
        全量重下（原来是 no-store），二次打开面板要等 2 秒以上。"""
        try:
            path = self.dir / fname
            mtime = path.stat().st_mtime
            hit = self._file_cache.get(fname)
            if hit is None or hit[0] != mtime:
                body = await asyncio.to_thread(path.read_bytes)
                etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
                self._file_cache[fname] = (mtime, body, etag)
            else:
                _, body, etag = hit
            headers = {
                # no-cache 而非 no-store：允许缓存但每次校验，ETag 命中即 304
                "Cache-Control": "no-cache",
                "ETag": etag,
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": CSP,
            }
            if request is not None and request.headers.get("If-None-Match") == etag:
                return web.Response(status=304, headers=headers)
            return web.Response(body=body, content_type=ctype, charset="utf-8", headers=headers)
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
