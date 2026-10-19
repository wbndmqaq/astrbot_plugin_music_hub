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

from astrbot.api import logger

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
        # 终态落定时刻：gc 对终态会话保留宽限期（见 gc 注释），WebUI 轮询要能取走结果
        self.finished_ts = 0.0
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

    def set_state(self, state: str, msg: str = "") -> bool:
        """单向状态机：已是终态（done/timeout/refuse/cancel）时拒绝被覆盖，返回是否生效。

        cancel 指令与驱动协程的完成判定之间没有别的同步手段：
        不设终态护栏的话，cancel 会把刚扫成的 done 抹成 cancel，
        _drive 的 finally 见 state != done 跳过 _finish，凭证被白白丢弃。
        """
        if self.state in ("done", "timeout", "refuse", "cancel"):
            return False
        self.state = state if state in STATES else "wait"
        self.msg = msg or _STATE_TEXT.get(self.state, "")
        if self.state in ("done", "timeout", "refuse", "cancel"):
            if self.finished_ts <= 0:
                self.finished_ts = time.time()
            self.done_evt.set()
        return True


class LoginFlows:
    def __init__(self, service):
        self._service = service  # core.service.MusicService
        self.sessions: dict[str, LoginSession] = {}
        # status() 惰性 gc 的节流时间戳（time.monotonic 基准），显式初始化免 getattr 兜底
        self._gc_ts = 0.0

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
        try:
            # 走 service.spawn 纳入统一生命周期：terminate 的 cancel-all 才能收到它，
            # 否则插件卸载后这个轮询器还会继续打上游登录接口直到自身超时
            session.task = self._service.spawn(self._drive(session))
        except Exception as e:
            # spawn 失败（如 TooManyTasks）时不能留下 task=None 的僵尸会话：
            # 二维码挂 600 秒无人回收、等待者永远等不到结果。置终态唤醒、
            # 撤掉登记，再以 ApiError 抛出（聊天端 handler 只接 ApiError）
            session.set_state("refuse", "登录服务繁忙，请稍后再试")
            self.sessions.pop(session.ticket, None)
            raise ApiError(f"登录服务繁忙，请稍后再试（{e}）", source=source) from e
        return session

    def get(self, ticket: str) -> LoginSession | None:
        return self.sessions.get(ticket)

    async def status(self, ticket: str) -> dict | None:
        # 先快照再 gc：gc 的回收（含终态弹出）不能抢在本轮询的应答前面，
        # 否则扫码结果只在「gc 刚好没触发」的窗口里可读
        s = self.sessions.get(ticket)
        snap = s.snapshot() if s else None
        # 轮询路径惰性 gc（60s 节流）：用户扫码中途放弃、前端只挂 status 轮询时，
        # 弃置会话（含整张二维码 base64）只有 start() 才有别的回收机会
        now = time.monotonic()
        if now - self._gc_ts > 60:
            self._gc_ts = now
            await self.gc()
        return snap

    async def cancel(self, ticket: str) -> None:
        s = self.sessions.get(ticket)
        if s is None:
            return
        # 竞态由 set_state 的单向状态机裁决：返回 False 说明驱动已判终态
        # （如扫码刚成功置 done），此时绝不能动 task——
        # _drive 的 finally 里 _finish 负责写配置，取消它会让凭证丢失
        if not s.set_state("cancel"):
            return
        if s.task:
            s.task.cancel()

    async def wait_done(self, session: LoginSession, timeout: float = MAX_WAIT) -> LoginSession | bool:
        """等待登录出结果；超时返回 False 且**不改状态**。

        超时若在此置终态 timeout，会抢在 _drive 之前盖死状态机：_drive 稍后
        拿到 cookie 的 set_state("done") 被拒绝，凭证到手却被丢弃。状态只能由
        _drive 判定。返回 session 时状态必为终态（done_evt 只在终态置位）。"""
        try:
            await asyncio.wait_for(session.done_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:  # 3.10 兼容：asyncio.TimeoutError 3.11 起才是内建 TimeoutError
            return False
        return session

    async def gc(self) -> None:
        """清掉过期/已完成的会话，并等待轮询任务真正退出。

        cancel() 只投递信号，不等落地就返回的话旧 _drive 仍会继续每 2 秒打上游，
        反复开扫码时会累积出大量在跑的轮询器。
        终态会话的任务**不 cancel**：done 的 _drive 正在 finally 里把凭证写盘
        （这是登录态唯一的持久化点），此刻取消等于白扫——改为等它自然收尾。

        终态会话保留 60s 宽限期再回收：原先 done 即刻可被 gc 弹出，status() 的
        惰性 gc（60s 节流）若恰好在 done 之后、前端下一次轮询之前触发，WebUI
        会拿到 None——用户看到的是「失败/过期」而凭证其实已写盘，白白重新扫码。
        宽限期内会话有界（终态后最多多留 60s），不构成无界累积。"""
        now = time.time()
        stale = [
            t
            for t, s in self.sessions.items()
            if now - s.created > 600
            or (s.done_evt.is_set() and (s.finished_ts <= 0 or now - s.finished_ts > 60))
        ]
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
            elif s.task and not s.task.done():
                # 终态收尾中（done 落盘 / 其余终态退出）：只等待，不打断
                tasks.append(s.task)
        if tasks:
            # 等待必须有界：_drive 依赖第三方库的事件流（qr_consume 等），异常形态下
            # 可能永不返回——无界 gather 会把 gc 挂死，而 start()/status() 的惰性
            # gc 都要过这里，扫码入口整体无响应。超时放弃等待，泄漏任务交给
            # terminate 的统一取消兜底
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*tasks, return_exceptions=True)), timeout=5
                )
            except asyncio.TimeoutError:
                logger.warning("[music_hub] 登录会话清理等待超时（5s），残留任务交由退出兜底")

    async def cancel_source(self, source: str) -> None:
        """同一音源只保留一个活跃会话：先取消旧的再让调用方新建。

        与 gc 同一纪律：cancel 只投递信号，不等旧轮询任务落地就返回的话，
        旧 _drive 仍会继续每 2 秒打上游登录接口。"""
        tasks: list[asyncio.Task] = []
        for st in list(self.sessions.values()):
            if st.source == source and st.state in ("wait", "scanned"):
                await self.cancel(st.ticket)
                # cancel 只在状态机接受（置成 cancel）时才真的取消了任务；
                # 已终态的任务要么已停要么在自旋收尾，不纳入等待
                if st.state == "cancel" and st.task and not st.task.done():
                    tasks.append(st.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            # 非 ApiError 原文可能带内部地址/库路径，不透给 WebUI 与聊天端
            logger.warning(f"[music_hub] 登录流程异常: {e}")
            session.set_state("refuse", "登录流程异常，请重试或查看运行日志")
        finally:
            if session.state == "done":
                await self._finish(session)

    async def _drive_poll(self, session: LoginSession) -> None:
        client = self._service.client_of(session.source)
        check = client.qr_check
        key = session.extra
        deadline = time.time() + MAX_WAIT
        notified_scan = False
        transient_fails = 0
        while time.time() < deadline:
            try:
                r = await check(key)
                transient_fails = 0
            except ApiError as e:
                # 单次网络抖动/上游瞬断不该废掉整个扫码窗口（最长等 5 分钟）：
                # 连续 3 次失败才判定登录服务不可用，间隔几秒重试
                transient_fails += 1
                if transient_fails >= 3:
                    session.set_state("refuse", e.user_msg())
                    return
                await asyncio.sleep(3)
                continue
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
                elif code == 3:
                    # 手机端点了「取消授权」：不映射会空转到超时，状态还错报成二维码超时
                    session.set_state("refuse", "已取消登录")
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

        qq_session = self._qq_session_of(session)
        if qq_session is None:
            # qr_start 异常形态下没拿到库会话对象：qr_consume(None) 行为不可控，直接判失败
            session.set_state("refuse", "登录会话初始化失败，请重试")
            return
        await client.qr_consume(qq_session, on_event)

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
        return await self._start_poll("ncm", "qrurl")

    async def _start_kg(self) -> LoginSession:
        return await self._start_poll("kg", "qrcode")

    async def _start_poll(self, source: str, url_field: str) -> LoginSession:
        """ncm/kg 共用的扫码会话创建：两者流程逐行相同，差异只在 qr_create 返回的
        二维码 URL 字段名（ncm=qrurl / kg=qrcode）。

        _start_ncm/_start_kg 保留为一行委托而非删除：它们是按音源命名的入口，
        也是测试的补丁点（test_consistency 以 _start_ncm 拦截真实客户端访问）。
        """
        client = self._service.client_of(source)
        key = await client.qr_key()
        qr = await client.qr_create(key)
        img = str(qr.get("qrimg") or "")
        if img.startswith("data:image"):
            img = img.split(",", 1)[-1]
        s = LoginSession(source, qr_b64=img, qr_url=str(qr.get(url_field) or ""))
        s.extra = key
        return s

    # ──────────── 收尾：写配置 ────────────
    async def _finish(self, session: LoginSession) -> None:
        source = session.source
        cookie = session.cookie
        if not cookie:
            # 能走到这里 state 必是终态 done（_drive 的 finally 判定过），
            # 单向状态机拒绝再改写，只能直接改文案提示异常
            session.msg = "登录成功但未取到凭证"
            return
        uid = session.uid
        nickname = session.nickname
        saved = False
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
            # 落盘是凭证唯一的持久化点：此刻即便外层任务被 cancel（terminate 等），
            # 也要让 save_async 跑完再响应取消。shield 只保证写盘本身不被打断，
            # 外层 await 仍会立即抛 CancelledError——这里接住它、等写盘任务真正
            # 落地后再重抛，否则 saved/msg 的赋值会被整段跳过（写盘成孤儿任务）
            save_task = asyncio.ensure_future(self._service.config.save_async())
            try:
                saved = bool(await asyncio.shield(save_task))
            except asyncio.CancelledError:
                try:
                    await save_task
                    saved = True
                except Exception:  # noqa: BLE001 - 取消路径只保证落盘尝试完成
                    saved = False
                session.uid = uid
                session.nickname = nickname
                session.msg = "登录成功" + ("" if saved else "（配置保存失败，仅本次会话生效）")
                raise
            session.uid = uid
            session.nickname = nickname
            session.msg = "登录成功" + ("" if saved else "（配置保存失败，仅本次会话生效）")
        except Exception as e:  # noqa: BLE001
            # 原文可能带本地路径/内部地址：session.msg 进 WebUI 轮询与聊天端，
            # 固定文案 + 日志留痕（对齐本模块 222 行「原文不透出」的纪律）
            logger.warning(f"[music_hub] {source} 登录成功但配置保存失败：{type(e).__name__}: {e}")
            session.msg = "登录成功但保存失败，详见运行日志"
        finally:
            # 回调放 finally：CancelledError 在写盘落地后穿透重抛时，原写法直接
            # 跳过回调——凭证已落盘但特权缓存刷新与成功日志缺失。以「状态 done 且
            # 已保存」为条件：保存失败/被取消打断的落盘不算登录成功。回调自身
            # 吞异常——finally 里再抛会遮蔽正在传播的原异常
            if session.state == "done" and saved:
                try:
                    self._service.on_login_success(source)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[music_hub] 登录成功回调异常: {e}")

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
