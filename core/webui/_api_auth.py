"""WebUI REST API handlers（认证与会话）。"""

from __future__ import annotations

from aiohttp import web

from ._auth import COOKIE_NAME
from ._util import body_json as _body
from ._util import json_response as _json


# ──────────── 认证 ────────────
def make_login(server):
    async def handler(request: web.Request) -> web.Response:
        ip = request.remote or ""
        body = await _body(request)
        # 先验密码再看锁：锁只拦错误试探，不拦持正确凭据的人。锁定按 peer IP
        # 计数，反代/共享出口部署下第三方输错就能锁死同出口的正常登录——
        # 密码正确时始终放行（攻击者不知道密码，此放宽不削弱防爆破）。
        if server.auth.check_password(str(body.get("password", ""))):
            server.auth.record_success(ip)
            token = server.auth.create_session(ip=ip)
            resp = _json({"ok": True})
            resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax", max_age=12 * 3600, path="/")
            return resp
        blocked = server.auth.login_blocked(ip)
        if blocked:
            return _json({"error": f"失败次数过多，{blocked} 秒后再试"}, 429)
        server.auth.record_fail(ip)
        return _json({"error": "密码错误"}, 401)

    return handler


def make_logout(server):
    async def handler(request: web.Request) -> web.Response:
        jti = request.get("jti", "")
        if jti:
            server.auth.revoke(jti)
        resp = _json({"ok": True})
        resp.del_cookie(COOKIE_NAME, path="/")
        return resp

    return handler


def make_change_password(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        # 先验旧密码：改密是有凭据的写操作，格式校验次序不应泄露"新密码策略"给未持凭据者
        if not server.auth.check_password(str(body.get("old", ""))):
            return _json({"error": "旧密码错误"}, 401)
        new_pwd = str(body.get("password", ""))
        if len(new_pwd) < 6:
            return _json({"error": "新密码至少 6 位"}, 400)
        node = dict(server.config.webui_node())
        node["password"] = new_pwd
        server.config.set("webui", node)
        saved = await server.config.save_async()
        # 改密后吊销其它会话
        server.auth.revoke_others(request.get("jti", ""))
        return _json({"ok": True, "saved": saved})

    return handler


def make_sessions(server):
    async def handler(request: web.Request) -> web.Response:
        return _json({"sessions": server.auth.list_sessions(request.get("jti", ""))})

    return handler


def make_revoke(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        sid = str(body.get("id", ""))
        if sid == "*":
            n = server.auth.revoke_others(request.get("jti", ""))
            return _json({"ok": True, "revoked": n})
        # UI 只展示 jti 前 8 位；这里按前 8 位精确比对，不做任意长度前缀匹配
        # （1 个字符的前缀会吊销第一个碰巧命中的、可能是别人的会话）。也接受完整 jti。
        target = next((j for j in server.auth.sessions if j == sid or j[:8] == sid), None)
        if not target:
            return _json({"error": "会话不存在"}, 404)
        server.auth.revoke(target)
        return _json({"ok": True})

    return handler
