"""WebUI REST API handlers（认证与会话）。"""

from __future__ import annotations

import asyncio

from aiohttp import web
from astrbot.api import logger

from ._auth import COOKIE_NAME, FAIL_DELAY, GateBusy, hash_password_async, verify_password_async
from ._util import body_json as _body
from ._util import json_response as _json

_TAG = "[music_hub]"
# 新密码长度上限：body 上限 1MB 内的超长「密码」会原样写进配置文件污染落盘
_PWD_MAX_BYTES = 128


def _ip_of(ip: str) -> str:
    return ip or "未知来源"


def _client_ip_of(request: web.Request, server) -> str:
    """登录限速 / 会话归属的来源 IP：直连取 TCP 对端，remote 命中 trustedProxy
    时取 X-Forwarded-For（统一实现在 _auth.client_ip）。"""
    return server.auth.client_ip(request.remote or "", request.headers.get("X-Forwarded-For", ""))


def _too_many(msg: str, retry_after: int) -> web.Response:
    resp = _json({"error": msg}, 429)
    # 排队上限/锁定拒绝必须带退避时长：调用方（与中间层限速器）无从得知何时重试
    resp.headers["Retry-After"] = str(max(1, int(retry_after)))
    return resp


# ──────────── 认证 ────────────
def make_login(server):
    async def handler(request: web.Request) -> web.Response:
        ip = _client_ip_of(request, server)
        # 单 IP 锁前置检查：PBKDF2 单次约 50-200ms 纯 CPU，已锁定的 IP 每个请求都
        # 先烧一次完整校验等于「锁定限速管猜测速率、管不住校验成本」——高并发随机
        # 密码即可把事件循环打满，阻塞整个 AstrBot。锁定中的 IP 一律 429，正确
        # 密码等锁定过期再登（5 分钟）。全局锁不在此拒绝（见 _auth.login_blocked）
        blocked = server.auth.login_blocked(ip)
        if blocked:
            logger.warning(f"{_TAG} WebUI 登录拒绝：锁定中（{_ip_of(ip)}，剩 {blocked}s）")
            return _too_many(f"失败次数过多，{blocked} 秒后再试", blocked)
        body = await _body(request)
        ok = False
        empty_stored = False
        try:
            async with server.auth.gate:
                # 空 stored 口令直接拒绝：明文回退路径 compare_digest(b"", b"") 恒真，
                # 不拦的话管理员在主面板清空密码字段后（live 即时生效）、插件重载重新
                # 生成口令前，任何人发空 body 登录即可获得管理员会话。
                # stored 必须在闸门内读取：闸门外捕获的旧哈希会让「排队期间改密」
                # 后仍按旧口令校验通过，且 create_session 按改密后口令算指纹——
                # 该会话改密完成后继续有效满 12h，违背「改密吊销全部旧会话」
                stored = str(server.auth.password_stored())
                if not stored:
                    empty_stored = True
                # 首次校验必须纳入闸门：放在闸门外会被未认证并发提交绕过排队直接
                # 烧线程池（600k 轮 PBKDF2 × N）。读取与校验同在闸门内，排队期间
                # 改密造成的复验问题不复存在；正确密码在闸门内校验成功即返回，
                # 不付 FAIL_DELAY 的延迟
                elif await verify_password_async(stored, str(body.get("password", ""))):
                    ok = True
                else:
                    await asyncio.sleep(FAIL_DELAY)
        except GateBusy:
            # 排队已满：并发失败提交占满闸门时快速拒绝，不把真实登录压到无界队尾
            return _too_many("登录请求过于频繁，请稍后再试", int(FAIL_DELAY) + 1)
        if empty_stored:
            logger.error(f"{_TAG} WebUI 登录拒绝：口令为空（请重载插件重新生成，{_ip_of(ip)}）")
            return _json({"error": "登录不可用：未设置密码，请重载插件重新生成"}, 401)
        if ok:
            server.auth.record_success(ip)
            token = server.auth.create_session(ip=ip)
            resp = _json({"ok": True})
            resp.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                samesite="Lax",
                secure=server.config.webui_secure_cookie,
                max_age=12 * 3600,
                path="/",
            )
            logger.info(f"{_TAG} WebUI 登录成功（{_ip_of(ip)}）")
            return resp
        server.auth.record_fail(ip)
        logger.warning(f"{_TAG} WebUI 登录失败（{_ip_of(ip)}）")
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
        ip = _client_ip_of(request, server)
        # 锁定前置（与 login 同口径：单 IP 锁定中的 IP 不再消耗 PBKDF2；全局锁不拒绝）
        blocked = server.auth.login_blocked(ip)
        if blocked:
            logger.warning(f"{_TAG} WebUI 改密拒绝：锁定中（{_ip_of(ip)}，剩 {blocked}s）")
            return _too_many(f"失败次数过多，{blocked} 秒后再试", blocked)
        body = await _body(request)
        new_pwd = str(body.get("password", ""))
        # 旧密码校验与 login 同口径：整个校验在闸门内完成（外置首验会被持有效
        # 会话的并发提交绕过排队烧线程池）；stored 在闸门内读取（与 login 同因：
        # 闸门外捕获的旧哈希在排队期间改密后仍是旧值），成功即返回，不付
        # FAIL_DELAY 的延迟
        old_ok = False
        empty_stored = False
        try:
            async with server.auth.gate:
                stored = str(server.auth.password_stored())
                if not stored:
                    empty_stored = True
                elif await verify_password_async(stored, str(body.get("old", ""))):
                    old_ok = True
                else:
                    await asyncio.sleep(FAIL_DELAY)
        except GateBusy:
            return _too_many("请求过于频繁，请稍后再试", int(FAIL_DELAY) + 1)
        if empty_stored:
            return _json({"error": "登录不可用：未设置密码，请重载插件重新生成"}, 401)
        if not old_ok:
            # 复用登录口的失败计数：持有效会话穷举旧密码时，login 的限速形同虚设
            logger.warning(f"{_TAG} WebUI 改密失败：旧密码错误（{_ip_of(ip)}）")
            server.auth.record_fail(ip)
            # 400 而非 401：前端把一切 401 当「会话过期」整站登出，
            # 业务性的「旧密码错误」不该把管理员踢出面板
            return _json({"error": "旧密码错误"}, 400)
        if len(new_pwd) < 6:
            return _json({"error": "新密码至少 6 位"}, 400)
        if len(new_pwd.encode("utf-8")) > _PWD_MAX_BYTES:
            return _json({"error": f"新密码过长（最多 {_PWD_MAX_BYTES} 字节）"}, 400)
        if new_pwd.isdigit() and len(new_pwd) < 8:
            # 纯数字短口令对 600k PBKDF2 仍是分钟级离线爆破（哈希随配置文件
            # 备份泄漏的场景）：拒绝最常见的弱口令形态
            return _json({"error": "纯数字密码至少 8 位，建议混入字母或符号"}, 400)
        # 先哈希再做 webui 节点读改写：dict(webui_node()) 与 config.set 之间不能有
        # await——并发的配置保存（webui 节点改 host/port 等）落在窗口内会被本次
        # 整体写回覆盖，新口令被静默回滚成旧哈希（改密假成功、旧口令继续有效）
        hashed = await hash_password_async(new_pwd)
        node = dict(server.config.webui_node())
        # 落盘只存 PBKDF2 哈希：配置文件被读到也拿不到口令本身（哈希在线程池算）
        node["password"] = hashed
        server.config.set("webui", node)
        # 凭证变更在 config.set 一刻已生效（AuthManager 每次校验都直读配置）：
        # 吊销其它会话并为当前 jti 重签新令牌，必须赶在下一个 await 之前同步完成
        server.auth.revoke_others(request.get("jti", ""))
        token = server.auth.reissue_session(request.get("jti", ""))
        server.auth.clear_initial_password_file()
        saved = await server.config.save_async()
        # 改密成功清掉本 IP 的失败计数（对齐 login.record_success 语义）
        server.auth.record_success(ip)
        # 审计留痕（对齐 config_save 口径：记动作 + 来源 IP，不记口令值）——
        # 被窃会话改密是账号接管信号，服务端零痕迹会让排查无从下手
        logger.info(f"{_TAG} WebUI 改密成功（{_ip_of(ip)}，落盘 {'成功' if saved else '失败'}）")
        resp = _json({"ok": True, "saved": saved})
        if token:
            # 当前会话无感续用：新令牌随本响应写回 cookie（HttpOnly/SameSite 等
            # 属性与登录签发路径完全一致），前端无需任何改动
            resp.set_cookie(
                COOKIE_NAME,
                token,
                httponly=True,
                samesite="Lax",
                secure=server.config.webui_secure_cookie,
                max_age=12 * 3600,
                path="/",
            )
        return resp

    return handler


def make_sessions(server):
    async def handler(request: web.Request) -> web.Response:
        return _json({"sessions": server.auth.list_sessions(request.get("jti", ""))})

    return handler


def make_revoke(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        sid = str(body.get("id", ""))
        ip = _client_ip_of(request, server)
        if sid == "*":
            n = server.auth.revoke_others(request.get("jti", ""))
            # 审计留痕：被窃会话批量吊销（含触发过 IP 告警的异常会话）是抹痕信号
            logger.info(f"{_TAG} WebUI 会话吊销：其它全部 ×{n}（{_ip_of(ip)}）")
            return _json({"ok": True, "revoked": n})
        # UI 只展示 jti 前 8 位；这里按前 8 位精确比对，不做任意长度前缀匹配
        # （1 个字符的前缀会吊销第一个碰巧命中的、可能是别人的会话）。也接受完整 jti。
        target = next((j for j in server.auth.sessions if j == sid or j[:8] == sid), None)
        if not target:
            return _json({"error": "会话不存在"}, 404)
        server.auth.revoke(target)
        logger.info(f"{_TAG} WebUI 会话吊销：{target[:8]}（{_ip_of(ip)}）")
        return _json({"ok": True})

    return handler
