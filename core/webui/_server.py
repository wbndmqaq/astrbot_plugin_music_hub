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
    "/webui/favicon.svg",
    "/webui/logos/ncm.png",
    "/webui/logos/kg.png",
    "/webui/logos/qq.png",
    "/api/meta",
    "/api/auth/login",
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
        # AuthManager 惰性到 start()：密钥文件读写与 Windows icacls（最坏 5s 子进程）
        # 都是同步阻塞，放线程池执行，不在事件循环里冷启动卡住 AstrBot 全部协程
        self.auth: AuthManager | None = None
        self._runner: web.AppRunner | None = None
        self.host = self.config.webui_host
        self.port = self.config.webui_port
        # 搜索结果歌曲缓存（source:sid → 完整归一化歌曲）：试听 / 远程投递取流用
        self._song_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        # 静态资源缓存：fname → (mtime, body, etag)
        self._file_cache: dict[str, tuple[float, bytes, str]] = {}
        # 并发连接闸（conn_limit_mw 用）：面板是单管理员工具，64 个在途请求远超
        # 任何真实用法；超限 503 快速拒绝而非排队。必须是普通计数而非
        # asyncio.Semaphore——acquire 耗尽时排队无限挂起，慢速 POST（headers 收完
        # 后滴漏 body）每连接钉死一槽即可让面板所有请求永久等待
        self._conn_max = 64
        self._conn_active = 0

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
        host = request.headers.get("Host", "")
        # 空 Host 一律 403：HTTP/1.1 客户端必须发送 Host 头，浏览器与 HTTP 库从不缺席，
        # 能触发空 Host 的只有手工构造的原始报文 —— 拦在调用侧，不会误伤正常访问
        if not host or not await self._host_ok(host):
            return _json({"error": "非法 Host：用域名访问面板需在插件配置 webui.hostAllowlist 登记"}, 403)
        # 非 GET 的 Origin 同源校验（防 CSRF）
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin", "")
            if origin:
                if not self._origin_ok(origin, host):
                    return _json({"error": "跨站请求被拒绝"}, 403)
            # 第三层 CSRF：Sec-Fetch-Site 由浏览器自动附加且禁止脚本伪造，
            # 值非同站直接拒绝（即使 Origin 被剥掉也拦得住）。该头只有现代浏览器才发，
            # 缺席（老客户端/非浏览器工具）放行，仍由 Origin 同源 + SameSite cookie 兜底
            site = request.headers.get("Sec-Fetch-Site", "")
            if site and site not in ("same-origin", "same-site", "none"):
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
            # 空 Host 已在 _guard 统一 403（HTTP/1.1 必带 Host）；这里放行
            # 只为兼容直调本方法的既有测试契约
            return True
        h = host.strip()
        if "@" in h:
            h = h.rsplit("@", 1)[1]
        if h.startswith("["):  # IPv6 字面量（[::1]:17818）
            if "]" not in h:
                # 畸形 IPv6（"[::1"）：静默截断会把 "::" 当合法主机名放行
                return False
            netloc = h[1 : h.find("]")].lower()
        else:
            netloc = h.split(":", 1)[0].lower()
        if not netloc:
            # ":8080" / "::"（非方括号）这类空主机名形态：字面严格性要求拒绝
            # （可利用面为零，但放行与白名单声明不符）
            return False
        if netloc == "localhost":
            return True
        try:
            ipaddress.ip_address(netloc)
            return True  # 字面 IP（私网/公网/回环）没有 DNS rebinding 向量，放行
        except ValueError:
            pass
        allowed = self.config.webui_host_allowlist
        # 精确匹配不做子域放行：登记宽泛域名（如裸注册域）会把面板暴露面
        # 扩大到其全部子域，多级子域/仿冒主机名都会被连带放行
        return netloc in allowed

    def _origin_ok(self, origin: str, host: str) -> bool:
        """Origin 必须与 Host 同源：hostname 必须全等，scheme/port 按部署形态比对。

        只比 hostname 会被端口差异绕过 —— SameSite 的同站点判定同样忽略端口，
        `localhost:3000` 与 `localhost:17818` 互为同站点，Lax cookie 照常携带，
        本机任意 web 服务都能发起带凭据的写请求（改 apiBase 即等于外泄平台 cookie）。
        用 urlparse 而非 split(":"):0 以正确剥离 IPv6 方括号。

        https 反代部署下浏览器的 Origin 是 https://，而 Host 头是反代原样透传的
        内网形态（http://127.0.0.1:17818）——scheme 严格比对会让所有 POST 必 403，
        把管理员推回 http 直连。故 secureCookie 开启（部署形态即 https）或登记了
        trustedProxy 时对 https 的 Origin 放行。放宽是安全的：CSRF 威胁模型里
        Origin 头由浏览器背书不可伪造，hostname 全等已挡住跨站；非浏览器工具
        本可同时伪造 Origin 与 Host，scheme 比对对它不构成额外成本。
        """
        try:
            o = urlparse(origin)
            h = urlparse(f"http://{host or ''}")
            if not o.hostname or not h.hostname:
                return False
            if o.hostname.lower() != h.hostname.lower():
                return False
            if o.scheme == "https" and (self.config.webui_secure_cookie or self.config.webui_trusted_proxy):
                return True
            if o.scheme != h.scheme:
                return False
            # 无显式端口时按 scheme 取默认端口，http/https 混用也要挡住
            o_port = o.port or (443 if o.scheme == "https" else 80)
            h_port = h.port or (443 if h.scheme == "https" else 80)
            return o_port == h_port
        except ValueError:
            return False

    def _authed(self, request: web.Request) -> bool:
        token = request.cookies.get(COOKIE_NAME, "")
        # 会话归属与 IP 变化告警用登录限速的同一来源口径（见 _auth.client_ip）：
        # 反代下不一致会让告警比对「代理 IP vs 真实客户端 IP」永远失配
        ip = self.auth.client_ip(request.remote or "", request.headers.get("X-Forwarded-For", ""))
        payload = self.auth.verify(token, ip)
        if payload:
            request["jti"] = payload.get("jti", "")
            request["user"] = payload.get("sub", "")
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

    def _apply_security_headers(self, resp: web.Response) -> None:
        """统一注入安全头。三条响应路径共用：常规路由、HTTPException 兜底渲染、
        conn_limit 的 503 快速拒绝（最外层中间件的响应不经过 security_headers_mw，
        不补头就会裸奔——漏 nosniff/XFO 且暴露 aiohttp 默认 Server 指纹）。"""
        # secureCookie 只在 https 部署下开启：此时补 HSTS 让浏览器首次响应后
        # 强制走 https（http 直连严禁下发——浏览器会把后续 http 访问硬升级，
        # 面板直接打不开）。逐请求读取：改配置重载插件重建 app 前后口径一致
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        # JSON/静态资源禁止被任意站点以 <img>/<script> 跨站嵌入探测
        resp.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        # 单管理员工具的纵深：隔离 window.opener 引用（v3 实测表曾误列 COOP，
        # 实际此前从未下发——现为真实启用）
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # 覆盖 aiohttp 默认的 "Python/x aiohttp/y"：版本指纹对定向攻击是免费情报
        resp.headers.setdefault("Server", "MusicHub")
        if bool(self.config.webui_secure_cookie):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")

    # ──────────── 应用 ────────────
    def _build_app(self) -> web.Application:
        @web.middleware
        async def guard_mw(request, handler):
            return await self._guard(request, handler)

        @web.middleware
        async def errors_mw(request, handler):
            return await self._handle_errors(request, handler)

        @web.middleware
        async def conn_limit_mw(request, handler):
            # 并发连接上限：改监听地址到非回环后，慢速连接（慢速读/慢速 POST）
            # 可以低成本占满协议端的连接槽。默认 127.0.0.1 时攻击者需先在本机，
            # 该限制是纵深而非主防线——超限快速 503，绝不排队（检查与自增之间无
            # await，事件循环单线程模型下没有竞态）
            if self._conn_active >= self._conn_max:
                resp = _json({"error": "服务器繁忙，请稍后重试"}, 503)
                self._apply_security_headers(resp)
                return resp
            self._conn_active += 1
            try:
                return await handler(request)
            finally:
                self._conn_active -= 1

        @web.middleware
        async def security_headers_mw(request, handler):
            # 统一注入：原先安全头只挂在 _file 上，所有 /api/* JSON 响应都没有，
            # 新增路由很容易漏挂
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                # 路由层抛出的 404/405 由 aiohttp 兜底渲染，不补头就会裸奔
                # （无 nosniff/DENY）；HTTPException 本身是 Response，直接改头再抛
                self._apply_security_headers(e)
                raise
            self._apply_security_headers(resp)
            return resp

        # client_max_size 显式钉在与 aiohttp 默认一致的 1MB：body_json 的 413 语义、
        # /api/resolve 的文本量级都建立在这条契约上，隐式依赖默认值容易被无意改掉
        # 中间件顺序：conn_limit 最外层（超限请求不进后续任何处理）
        app = web.Application(
            middlewares=[conn_limit_mw, security_headers_mw, errors_mw, guard_mw], client_max_size=1024**2
        )
        r = app.router
        # 静态
        r.add_get("/", self._index)
        r.add_get("/webui/style.css", self._static("style.css", "text/css"))
        r.add_get("/webui/app.js", self._static("app.js", "application/javascript"))
        r.add_get("/webui/theme-boot.js", self._static("theme-boot.js", "application/javascript"))
        r.add_get("/webui/favicon.svg", self._static("favicon.svg", "image/svg+xml"))
        # 三平台官方标识（侧边栏 / 音源标签用）
        for _name in ("ncm", "kg", "qq"):
            r.add_get(f"/webui/logos/{_name}.png", self._static(f"logos/{_name}.png", "image/png"))
        # 元信息 / 认证
        r.add_get("/api/meta", self._meta)
        r.add_post("/api/auth/login", _api.make_login(self))
        r.add_post("/api/auth/logout", _api.make_logout(self))
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

    async def _ensure_auth(self) -> None:
        if self.auth is None:
            self.auth = await asyncio.to_thread(
                AuthManager,
                self.service.data_dir,
                lambda: self.config.webui_password,
                # 来源 IP 解析的可信反代配置：惰性读取，改配置无需重建 AuthManager
                lambda: self.config.webui_trusted_proxy,
                # 会话有效期（webui.sessionHours）：同上惰性读取，改完对后续登录生效
                lambda: self.config.webui_session_hours,
            )

    async def start(self) -> None:
        if self._runner is not None:
            return
        await self._ensure_auth()
        app = self._build_app()
        self._runner = web.AppRunner(
            app, access_log=None, shutdown_timeout=SHUTDOWN_TIMEOUT, keepalive_timeout=15
        )
        # setup/start 的任何异常路径都必须清理 _runner 并置回 None：
        # 残留会让重试在入口「已启动」分支静默短路，面板看似在跑实则没起，
        # 只能靠重载插件自救
        try:
            await self._runner.setup()
            site = web.TCPSite(self._runner, self.host, self.port)
            await site.start()
        except OSError as e:
            await self._runner.cleanup()
            self._runner = None
            raise RuntimeError(f"WebUI 端口 {self.port} 启动失败：{e}") from e
        except BaseException:
            # 非 OSError（含 CancelledError）同样清理后再抛，保证可重试
            await self._runner.cleanup()
            self._runner = None
            raise
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
        authed = self._authed(request)
        return _json(
            {
                "name": DISPLAY_NAME,
                "plugin": "astrbot_plugin_music_hub",
                # 版本号只对已登录会话暴露：/api/meta 是公开路由，未登录即可查询
                # 的精确版本等于给「对照已知漏洞定向攻击」递情报；登录页只用 name
                "version": self._version() if authed else "",
                "authed": authed,
            }
        )

    @staticmethod
    def _version() -> str:
        try:
            from ..help_data import VERSION

            return VERSION
        except Exception:  # noqa: BLE001
            return ""
