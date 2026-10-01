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
        blocked = server.auth.login_blocked(ip)
        if blocked:
            return _json({"error": f"失败次数过多，{blocked} 秒后再试"}, 429)
        body = await _body(request)
        if not server.auth.check_password(str(body.get("password", ""))):
            server.auth.record_fail(ip)
            return _json({"error": "密码错误"}, 401)
        server.auth.record_success(ip)
        token = server.auth.create_session()
        resp = _json({"ok": True})
        resp.set_cookie(COOKIE_NAME, token, httponly=True, samesite="Lax", max_age=12 * 3600, path="/")
        return resp

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


def make_check(server):
    async def handler(request: web.Request) -> web.Response:
        return _json({"authed": bool(request.get("jti")), "user": request.get("user", "")})

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
        target = next((j for j in server.auth.sessions if j.startswith(sid)), None)
        if not target:
            return _json({"error": "会话不存在"}, 404)
        server.auth.revoke(target)
        return _json({"ok": True})

    return handler
