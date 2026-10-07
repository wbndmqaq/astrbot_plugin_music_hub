"""扫码登录管理器：三平台统一会话模型。

- ncm / kg：HTTP 轮询（qr_key → qr_create → qr_check）
- qq：qqmusic-api 事件流后台消费

同一套 LoginSession 同时服务聊天指令（发图 + 等待完成）与 WebUI（票据轮询）。
凭证写入插件配置（ncm/kg: cookie；qq: credential JSON）。
"""

from __future__ import annotations

import asyncio
import secrets
import time

from .errors import ApiError

POLL_INTERVAL = 2.0
MAX_WAIT = 300.0
STATES = ("wait", "scanned", "done", "timeout", "refuse", "cancel")

_STATE_TEXT = {
    "wait": "等待扫码",
    "scanned": "已扫码，请在手机上确认",
    "done": "登录成功",
    "timeout": "二维码超时",
    "refuse": "登录被取消或失败",
    "cancel": "已取消",
}


class LoginSession:
    def __init__(self, source: str, qr_b64: str = "", qr_url: str = "", extra: str = ""):
        self.ticket = secrets.token_urlsafe(12)
        self.source = source
        self.state = "wait"
        self.qr_b64 = qr_b64  # base64 PNG（不含 data: 前缀）
        self.qr_url = qr_url
        self.extra = extra  # kg: key；qq: login_type
        self.msg = _STATE_TEXT["wait"]
        self.cookie = ""
        self.uid = ""
        self.nickname = ""
        self.created = time.time()
        self.done_evt: asyncio.Event = asyncio.Event()
        self.task: asyncio.Task | None = None
        # kg QQ 扫码上下文
        self.poll_ctx: dict = {}

    def snapshot(self) -> dict:
        return {
            "ticket": self.ticket,
            "source": self.source,
            "state": self.state,
            "qr": f"data:image/png;base64,{self.qr_b64}" if self.qr_b64 else "",
            "qrUrl": self.qr_url,
            "msg": self.msg,
            "nickname": self.nickname,
            "uid": self.uid,
            "age": int(time.time() - self.created),
        }

    def set_state(self, state: str, msg: str = "") -> None:
        self.state = state if state in STATES else "wait"
        self.msg = msg or _STATE_TEXT.get(self.state, "")
        if state in ("done", "timeout", "refuse", "cancel"):
            self.done_evt.set()


class LoginFlows:
    def __init__(self, service):
        self._service = service  # core.service.MusicService
        self.sessions: dict[str, LoginSession] = {}

    # ──────────── 对外 ────────────
    async def start(self, source: str, login_type: str = "qq") -> LoginSession:
        """创建扫码会话并启动后台轮询。"""
        await self.gc()
        # 同一音源已有的待扫码会话先取消：gc 只清超 600 秒或已完成的，
        # 刚开的那一个不满足条件，不主动取消就会和新的一起轮询上游。
        await self.cancel_source(source)
        if source == "qq":
            session = await self._start_qq(login_type)
        elif source == "ncm":
            session = await self._start_ncm()
        elif source == "kg":
            session = await self._start_kg()
        else:
            raise ApiError(f"未知音源 {source}", source=source)
        self.sessions[session.ticket] = session
        # 走 service.spawn 纳入统一生命周期：terminate 的 cancel-all 才能收到它，
        # 否则插件卸载后这个轮询器还会继续打上游登录接口直到自身超时
        session.task = self._service.spawn(self._drive(session))
        return session

    def get(self, ticket: str) -> LoginSession | None:
        return self.sessions.get(ticket)

    async def status(self, ticket: str) -> dict | None:
        s = self.sessions.get(ticket)
        return s.snapshot() if s else None

    async def cancel(self, ticket: str) -> None:
        s = self.sessions.get(ticket)
        # 已完成的会话不能动 task：_drive 的 finally 里 _finish 负责写配置，
        # 取消它会让 cookie 丢失而状态仍显示 done。
        if s is None or s.state not in ("wait", "scanned"):
            return
        s.set_state("cancel")
        if s.task:
            s.task.cancel()

    async def wait_done(self, session: LoginSession, timeout: float = MAX_WAIT) -> LoginSession:
        try:
            await asyncio.wait_for(session.done_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:  # 3.10 兼容：asyncio.TimeoutError 3.11 起才是内建 TimeoutError
            session.set_state("timeout")
        return session

    async def gc(self) -> None:
        """清掉过期/已完成的会话，并等待轮询任务真正退出。

        cancel() 只投递信号，不等落地就返回的话旧 _drive 仍会继续每 2 秒打上游，
        反复开扫码时会累积出大量在跑的轮询器。
        """
        now = time.time()
        stale = [t for t, s in self.sessions.items() if now - s.created > 600 or s.done_evt.is_set()]
        tasks: list[asyncio.Task] = []
        for t in stale:
            s = self.sessions.pop(t, None)
            if s is None:
                continue
            if s.state in ("wait", "scanned"):
                s.set_state("cancel")  # 唤醒仍在等 done_evt 的调用方
            if s.task and not s.task.done():
                s.task.cancel()
                tasks.append(s.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def cancel_source(self, source: str) -> None:
        """同一音源只保留一个活跃会话：先取消旧的再让调用方新建。"""
        for st in list(self.sessions.values()):
            if st.source == source and st.state in ("wait", "scanned"):
                await self.cancel(st.ticket)

    def active_of(self, source: str) -> LoginSession | None:
        for s in self.sessions.values():
            if s.source == source and s.state in ("wait", "scanned"):
                return s
        return None

    # ──────────── 后台驱动 ────────────
    async def _drive(self, session: LoginSession) -> None:
        try:
            if session.source == "qq":
                await self._drive_qq(session)
            else:
                await self._drive_poll(session)
        except asyncio.CancelledError:
            raise
        except ApiError as e:
            session.set_state("refuse", e.user_msg())
        except Exception as e:  # noqa: BLE001 - 登录失败不能崩插件
            session.set_state("refuse", f"登录流程异常：{e}")
        finally:
            if session.state == "done":
                await self._finish(session)

    async def _drive_poll(self, session: LoginSession) -> None:
        client = self._service.client_of(session.source)
        check = client.qr_check
        key = session.extra
        deadline = time.time() + MAX_WAIT
        notified_scan = False
        while time.time() < deadline:
            try:
                r = await check(key)
            except ApiError as e:
                session.set_state("refuse", e.user_msg())
                return
            code = r.get("code")
            if session.source == "ncm":
                if code == 802 and not notified_scan:
                    notified_scan = True
                    session.set_state("scanned")
                elif code == 803:
                    session.cookie = r.get("cookie") or ""
                    session.set_state("done")
                    return
                elif code == 800:
                    session.set_state("timeout")
                    return
            else:  # kg
                if code == 2 and not notified_scan:
                    notified_scan = True
                    session.set_state("scanned")
                elif code == 4:
                    session.cookie = r.get("cookie") or ""
                    session.uid = str(r.get("uid") or "")
                    session.nickname = str(r.get("nickname") or "")
                    session.set_state("done")
                    return
                elif code == 0:
                    session.set_state("timeout")
                    return
            await asyncio.sleep(POLL_INTERVAL)
        session.set_state("timeout")

    async def _drive_qq(self, session: LoginSession) -> None:
        client = self._service.client_of("qq")

        async def on_event(state: str, payload: str) -> None:
            if state == "scanned":
                session.set_state("scanned")
            elif state == "done":
                session.cookie = payload
                session.set_state("done")
            else:
                session.set_state(state)

        await client.qr_consume(self._qq_session_of(session), on_event)

    def _qq_session_of(self, session: LoginSession):
        """qq 会话对象存在 session.poll_ctx（qr_start 时塞入）。"""
        return session.poll_ctx.get("session")

    async def _start_qq(self, login_type: str) -> LoginSession:
        client = self._service.client_of("qq")
        r = await client.qr_start(login_type)
        s = LoginSession("qq", qr_b64=r.get("qrB64", ""), extra=login_type)
        s.poll_ctx["session"] = r.get("session")
        return s

    async def _start_ncm(self) -> LoginSession:
        client = self._service.client_of("ncm")
        key = await client.qr_key()
        qr = await client.qr_create(key)
        img = str(qr.get("qrimg") or "")
        if img.startswith("data:image"):
            img = img.split(",", 1)[-1]
        s = LoginSession("ncm", qr_b64=img, qr_url=str(qr.get("qrurl") or ""))
        s.extra = key
        return s

    async def _start_kg(self) -> LoginSession:
        client = self._service.client_of("kg")
        key = await client.qr_key()
        qr = await client.qr_create(key)
        img = str(qr.get("qrimg") or "")
        if img.startswith("data:image"):
            img = img.split(",", 1)[-1]
        s = LoginSession("kg", qr_b64=img, qr_url=str(qr.get("qrcode") or ""))
        s.extra = key
        return s

    # ──────────── 收尾：写配置 ────────────
    async def _finish(self, session: LoginSession) -> None:
        source = session.source
        cookie = session.cookie
        if not cookie:
            session.set_state("refuse", "登录成功但未取到凭证")
            return
        uid = session.uid
        nickname = session.nickname
        try:
            if source == "qq":
                data = _json_load(cookie)
                uid = str(data.get("musicid") or "")
                self._service.config.set_src_cookie("qq", cookie, uid)
            else:
                if not uid or not nickname:
                    status = await self._safe_status(source)
                    uid = uid or status.get("uid", "")
                    nickname = nickname or status.get("nickname", "")
                self._service.config.set_src_cookie(source, cookie, uid)
            saved = await self._service.config.save_async()
            session.uid = uid
            session.nickname = nickname
            session.msg = "登录成功" + ("" if saved else "（配置保存失败，仅本次会话生效）")
        except Exception as e:  # noqa: BLE001
            session.msg = f"登录成功但保存失败：{e}"
        self._service.on_login_success(source)

    async def _safe_status(self, source: str) -> dict:
        try:
            return await self._service.client_of(source).login_status()
        except Exception:  # noqa: BLE001
            return {}


def _json_load(text: str) -> dict:
    import json

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}
