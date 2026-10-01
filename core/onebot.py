"""OneBot(aiocqhttp) 底层直发：base64 语音/文件、原生音乐小程序卡。

绕过 AstrBot 的 WAV 强转与跨容器文件系统限制；协议端自行完成 silk 转码。
"""

from __future__ import annotations

import asyncio
import os

from astrbot.api import logger

from . import SOURCE_KG, SOURCE_NCM, SOURCE_QQ

TAG = "[music_hub]"
_BASE64_INLINE_MAX_BYTES = 150 * 1024 * 1024


# ──────────── OneBot 直发 ────────────
def _file_to_base64(path: str) -> str:
    import base64

    size = os.path.getsize(path)
    if size > _BASE64_INLINE_MAX_BYTES:
        raise RuntimeError(f"文件 {size // 1024 // 1024}MB 超过 base64 直发上限")
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


async def _aiocq_call_action(event, action: str, sid: int, segs: list) -> None:
    bot = getattr(event, "bot", None) or getattr(getattr(event, "platform", None), "bot", None)
    if bot is None:
        raise RuntimeError("无法获取 aiocqhttp bot 实例")
    if action == "send_group_msg":
        await bot.call_action(action, group_id=int(sid), message=segs)
    else:
        await bot.call_action(action, user_id=int(sid), message=segs)


def _aiocq_target(event) -> tuple[str, int]:
    is_group = bool(getattr(event.message_obj, "group_id", None))
    sid = event.message_obj.group_id if is_group else event.get_sender_id()
    return ("send_group_msg" if is_group else "send_private_msg"), int(sid)


async def _aiocq_send_file(event, text: str, display: str, path: str) -> None:
    b64 = await asyncio.to_thread(_file_to_base64, path)
    segs: list = []
    if text:
        segs.append({"type": "text", "data": {"text": text}})
    segs.append({"type": "file", "data": {"file": f"base64://{b64}", "name": display}})
    action, sid = _aiocq_target(event)
    await _aiocq_call_action(event, action, sid, segs)


async def _aiocq_send_record(event, text: str, src_path: str) -> tuple[bool, str]:
    try:
        b64 = await asyncio.to_thread(_file_to_base64, src_path)
        segs: list = []
        if text:
            segs.append({"type": "text", "data": {"text": text}})
        segs.append({"type": "record", "data": {"file": f"base64://{b64}"}})
        action, sid = _aiocq_target(event)
        await _aiocq_call_action(event, action, sid, segs)
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


async def _send_music_segment(event, music_data: dict) -> bool:
    bot = getattr(event, "bot", None) or getattr(getattr(event, "platform", None), "bot", None)
    call_action = getattr(bot, "call_action", None)
    if call_action is None:
        return False
    action, sid = _aiocq_target(event)
    target = {"group_id": sid} if action == "send_group_msg" else {"user_id": sid}
    try:
        await call_action(action, message=[{"type": "music", "data": music_data}], **target)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{TAG} 音乐卡片发送失败: {e}")
        return False


_NATIVE_CARD_TYPE = {SOURCE_QQ: "qq", SOURCE_KG: "kugou", SOURCE_NCM: "163"}


async def send_native_music_card(event, source: str, song: dict) -> bool:
    """原生音乐小程序卡（QQ/酷狗/网易云），仅 OneBot 平台。"""
    card_type = _NATIVE_CARD_TYPE.get(source)
    if not card_type:
        return False
    # 音乐卡要的是平台数字歌曲 id：ncm = sid；qq = sid2（数字 id，sid 是 mid）；
    # kg 用 32 位文件 hash（sid）
    if source == SOURCE_NCM:
        native_id = song.get("sid") or song.get("sid2")
    elif source == SOURCE_QQ:
        native_id = song.get("sid2") or song.get("sid")
    else:
        native_id = song.get("sid") or song.get("sid2")
    if not native_id:
        return False
    return await _send_music_segment(event, {"type": card_type, "id": str(native_id)})
