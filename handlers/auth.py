"""登录 / 状态 / 登出路由。"""

from __future__ import annotations

import asyncio
import re

from astrbot.api.message_components import Image

from ..core import SOURCE_NAMES, SOURCES
from ..core.cards import build_status_card_data
from ..core.errors import ApiError
from .base import Route

_SRC = r"(ncm|kg|qqm|qq)"

_RE_LOGIN = rf"^\s*#?{_SRC}\s*(?:扫码)?登录\s*(qq|微信|wx|app)?\s*$"
_RE_LOGOUT = rf"^\s*#?{_SRC}\s*(?:登出|注销|解绑)\s*$"
_RE_STATUS = rf"^\s*#?(?:音乐|mh)?\s*({_SRC})?\s*(?:登录)?状态\s*$"


def _src_of(token: str | None) -> str:
    from ..core.sources import token_to_source

    return token_to_source(token, default="")


async def run_login(service, event):
    m = re.search(_RE_LOGIN, event.message_str, re.IGNORECASE)
    src = _src_of(m.group(1) if m else "")
    sub = (m.group(2) if m else "").strip().lower()
    if not src:
        await service.reply(event, "用法：ncm登录 / kg登录 / qq登录")
        return
    if not service.config.enable:
        await service.reply(event, "插件已关闭")
        return
    try:
        session = await service.login.start(src, sub or "qq")
    except ApiError as e:
        await service.reply(event, e.with_source())
        return
    # 发二维码
    if session.qr_b64:
        import base64
        import os

        from ..core.delivery import get_temp_dir

        try:
            d = await get_temp_dir()
            path = os.path.join(d, f"qr_{session.ticket}.png")
            with open(path, "wb") as f:
                f.write(base64.b64decode(session.qr_b64))
            await service.send_chain(event, Image.fromFileSystem(path))
            service.schedule_unlink(path, 180)
        except Exception as e:  # noqa: BLE001
            service.log_warn(f"二维码图片发送失败: {e}")
    if session.qr_url and not session.qr_b64:
        await service.reply(event, f"请打开链接扫码登录：{session.qr_url}")
    await service.reply(
        event, f"[{SOURCE_NAMES[src]}] 请使用手机 App 扫码，超时 5 分钟。登录完成后自动提示。"
    )
    done = await service.login.wait_done(session)
    if done.state == "done":
        who = f"（{done.nickname}）" if done.nickname else ""
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 登录成功{who}，已保存登录态")
    elif done.state == "scanned":
        await service.reply(event, "扫码确认超时，请重新登录")
    else:
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 登录未完成：{done.msg}")


async def run_logout(service, event):
    m = re.search(_RE_LOGOUT, event.message_str, re.IGNORECASE)
    src = _src_of(m.group(1) if m else "")
    if not src:
        return
    try:
        if src != "qq":
            client = service.client_of(src)
            if hasattr(client, "logout"):
                await client.logout()
        else:
            await service.qq.logout()
        service.config.clear_src_cookie(src)
        await service.config.save_async()
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 已退出登录")
    except Exception as e:  # noqa: BLE001
        await service.reply(event, f"退出登录失败：{e}")


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
        status["nickname"] = f"查询失败：{e}"
    vip, grade = await asyncio.gather(_safe(service.vip_summary(src)), _safe(service.grade_summary(src)))
    if grade:
        status["vip"] = f"{vip} · {grade}" if vip else grade
    elif vip:
        status["vip"] = vip
    return status


async def run_status(service, event):
    if not service.config.enable:
        await service.reply(event, "插件已关闭")
        return
    m = re.search(_RE_STATUS, event.message_str, re.IGNORECASE)
    only = _src_of(m.group(1) if m else "")
    targets = [s for s in SOURCES if not only or s == only]
    rows = await asyncio.gather(*(_status_row(service, s) for s in targets))
    data = build_status_card_data(list(rows))
    await service.reply_card_or_text(event, data, "status", only or "auto", _format_status_text)


def _format_status_text(data: dict) -> str:
    lines = ["♪ Music Hub 登录状态"]
    for s in data.get("sources", []):
        state = "已登录" if s.get("loggedIn") else "未登录"
        nick = f"（{s['nickname']}）" if s.get("nickname") and s.get("loggedIn") else ""
        if s.get("nickname") and not s.get("loggedIn"):
            nick = f"（{s['nickname']}）"
        vip = f" · {s['vip']}" if s.get("vip") else ""
        lines.append(f"· {s.get('name', '')}：{state}{nick} · 音质 {s.get('quality', '')}{vip}")
    lines.append("扫码登录：ncm登录 / kg登录 / qq登录")
    return "\n".join(lines)


def routes() -> list[Route]:
    return [
        Route(
            re.compile(_RE_LOGIN, re.IGNORECASE), "mh_login", "扫码登录", run_login, admin=True, priority=7
        ),
        Route(
            re.compile(_RE_LOGOUT, re.IGNORECASE), "mh_logout", "退出登录", run_logout, admin=True, priority=7
        ),
        Route(re.compile(_RE_STATUS, re.IGNORECASE), "mh_status", "登录状态", run_status, priority=7),
    ]
