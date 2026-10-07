"""登录 / 状态 / 登出路由。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from astrbot.api.message_components import Image

from ..core import SOURCE_NAMES, SOURCES
from ..core.cards import build_status_card_data
from ..core.errors import ApiError, TooManyTasks
from ..core.sources import token_to_source
from .base import Route

_SRC = r"(ncm|kg|qqm|qq)"

_RE_LOGIN = rf"^\s*#?{_SRC}\s*(?:扫码)?登录\s*(qq|微信|wx|app)?\s*$"
_RE_LOGOUT = rf"^\s*#?{_SRC}\s*(?:登出|注销|解绑)\s*$"
_RE_STATUS = rf"^\s*#?(?:音乐|mh)?\s*({_SRC})?\s*(?:登录)?状态\s*$"


async def _login_finish(service, src: str, umo: str, session) -> None:
    """后台等扫码结果并主动推送。cancel 静默：用户重新发起登录时旧会话被
    cancel_source 取消，此时不该给会话里塞一条「登录未完成」的噪音。"""
    done = await service.login.wait_done(session)
    if done is False:
        # 等待超时：状态留给 _drive 判定（它自己的超时随后置终态），这里只提示
        await service.send_to_umo(umo, "扫码确认超时，请重新登录")
        return
    if done.state == "done":
        who = f"（{done.nickname}）" if done.nickname else ""
        await service.send_to_umo(umo, f"[{SOURCE_NAMES[src]}] 登录成功{who}，已保存登录态")
    elif done.state != "cancel":
        await service.send_to_umo(umo, f"[{SOURCE_NAMES[src]}] 登录未完成：{done.msg}")


async def run_login(service, event):
    m = re.search(_RE_LOGIN, event.message_str, re.IGNORECASE)
    src = token_to_source(m.group(1) if m else "", default="")
    sub = (m.group(2) if m else "").strip().lower()
    if not src:
        await service.reply(event, "用法：ncm登录 / kg登录 / qq登录")
        return
    # 不查总开关：schema 承诺登录类指令不受影响，且路由本就 gated=False
    try:
        session = await service.login.start(src, sub or "qq")
    except ApiError as e:
        await service.reply(event, e.with_source())
        return
    # 发二维码
    if session.qr_b64:
        import base64

        from ..core.delivery import get_temp_dir

        try:
            path = str(Path(await get_temp_dir()) / f"qr_{session.ticket}.png")
            # 二维码几十 KB，落盘走线程池，不占事件循环
            await asyncio.to_thread(Path(path).write_bytes, base64.b64decode(session.qr_b64))
            # 清理先于发送登记：send_chain 对含媒体链失败会重新抛出，若把登记放在
            # 发送之后，失败路径这张 PNG 永远不会被清
            service.schedule_unlink(path, 180)
            await service.send_chain(event, Image.fromFileSystem(path))
        except Exception as e:  # noqa: BLE001
            service.log_warn(f"二维码图片发送失败: {e}")
    if session.qr_url and not session.qr_b64:
        await service.reply(event, f"请打开链接扫码登录：{session.qr_url}")
    await service.reply(
        event, f"[{SOURCE_NAMES[src]}] 请使用手机 App 扫码，超时 5 分钟。登录完成后自动提示。"
    )
    # 等 5 分钟不能挂在本 handler 里：插件重载后旧 handler 空等满超时，
    # 用户也永远收不到回执。放后台等，完成后按 umo 主动推送（与点歌台/订阅同机制）。
    try:
        service.spawn(_login_finish(service, src, event.unified_msg_origin, session))
    except TooManyTasks:
        await service.reply(event, "系统繁忙，登录结果可能无法推送，请稍后用「状态」确认")


async def run_logout(service, event):
    m = re.search(_RE_LOGOUT, event.message_str, re.IGNORECASE)
    src = token_to_source(m.group(1) if m else "", default="")
    if not src:
        return
    try:
        client = service.client_of(src)
        if hasattr(client, "logout"):
            await client.logout()
        service.config.clear_src_cookie(src)
        await service.config.save_async()
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 已退出登录")
    except ApiError as e:
        # user_msg 是面向聊天的文案；原始异常串可能带上游细节，不回群聊
        await service.reply(event, f"退出登录失败：{e.user_msg()}")
    except Exception as e:  # noqa: BLE001
        service.log_warn(f"登出({src})失败: {e}")
        await service.reply(event, f"退出登录失败：{type(e).__name__}")


async def _safe(coro, default=""):
    """状态汇总里任何单项失败只降级该项，不拖垮整卡。"""
    try:
        return await coro
    except Exception:  # noqa: BLE001
        return default


async def _status_row(service, src: str) -> dict:
    """一个音源的状态行。登录态与 VIP/等级并发查——三平台串行要等九次往返。"""
    status = {
        "source": src,
        "name": SOURCE_NAMES[src],
        "loggedIn": False,
        "nickname": "",
        "uid": "",
        "quality": service.config.src_quality(src),
        "enabled": service.config.src_enabled(src),
        "avatar": "",
    }
    if not service.config.src_enabled(src):
        status["nickname"] = "未配置 API"
        return status
    try:
        status.update(await service.client_of(src).login_status())
    except ApiError as e:
        status["nickname"] = f"查询失败：{e.user_msg()}"
    except Exception as e:  # noqa: BLE001
        service.log_warn(f"login_status({src}) 查询失败: {e}")
        status["nickname"] = "查询失败：上游无响应"
    vip, grade = await asyncio.gather(_safe(service.vip_summary(src)), _safe(service.grade_summary(src)))
    if grade:
        status["vip"] = f"{vip} · {grade}" if vip else grade
    elif vip:
        status["vip"] = vip
    return status


async def run_status(service, event):
    # 不查总开关：与 run_login 同一 schema 承诺（登录类指令不受影响）
    m = re.search(_RE_STATUS, event.message_str, re.IGNORECASE)
    only = token_to_source(m.group(1) if m else "", default="")
    targets = [s for s in SOURCES if not only or s == only]
    rows = await asyncio.gather(*(_status_row(service, s) for s in targets))
    data = build_status_card_data(list(rows))
    await service.reply_card_or_text(event, data, "status", only or "auto", _format_status_text)


def _format_status_text(data: dict) -> str:
    lines = ["♪ Music Hub 登录状态"]
    for s in data.get("sources", []):
        state = "已登录" if s.get("loggedIn") else "未登录"
        # 未登录时 nickname 存的是"未配置 API"/"查询失败：..."等提示，一并显示更有用
        nick = f"（{s['nickname']}）" if s.get("nickname") else ""
        vip = f" · {s['vip']}" if s.get("vip") else ""
        lines.append(f"· {s.get('name', '')}：{state}{nick} · 音质 {s.get('quality', '')}{vip}")
    lines.append("扫码登录：ncm登录 / kg登录 / qq登录")
    return "\n".join(lines)


def routes() -> list[Route]:
    return [
        Route(
            re.compile(_RE_LOGIN, re.IGNORECASE),
            "mh_login",
            "扫码登录（管理员）",
            run_login,
            admin=True,
            priority=7,
        ),
        Route(
            re.compile(_RE_LOGOUT, re.IGNORECASE),
            "mh_logout",
            "退出登录（管理员）",
            run_logout,
            admin=True,
            priority=7,
        ),
        Route(
            re.compile(_RE_STATUS, re.IGNORECASE),
            "mh_status",
            "登录状态（管理员）",
            run_status,
            # 登录昵称 / uid / VIP 属账号信息，与管理类指令同门槛
            admin=True,
            priority=7,
        ),
    ]
