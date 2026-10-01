"""远程投递：把歌从 WebUI / 点歌台队列 / 订阅推送送到指定会话（无 event 对象场景）。

发送通道走 ``context.send_message(umo, MessageChain)``：语音优先（按平台能力矩阵），
失败转群文件，再失败退文本+直链。主动消息支持矩阵见 core.platform_caps。
"""

from __future__ import annotations

import os

from astrbot.api import logger

from . import SOURCE_NAMES
from .media import (
    _VOCAL_DIRECT_EXT,
    _schedule_cleanup,
    build_music_filename,
    download_audio,
    get_temp_dir,
    prepare_vocal_file,
)
from .platform_caps import caps_for_name

TAG = "[music_hub]"


async def send_audio_to(service, umo: str, song: dict, play: dict, *, note: str = "") -> dict:
    """把已取流（或未取流会现场 resolve）的歌曲投递到指定会话。

    play 可传 {} —— 内部会先 resolve_play 取流。返回 {ok, reason}。
    """
    source = song.get("source", "")
    if not play or not play.get("url"):
        play = await service.resolve_play(song)
    label = play.get("label") or play.get("qualityLabel") or ""
    src_name = SOURCE_NAMES.get(source, source)
    prefix = f"{note} " if note else ""
    text = (
        f"{prefix}♪ {song.get('name')} - {song.get('artist')} · {src_name}{(' · ' + label) if label else ''}"
    )
    url = play.get("url") or ""
    if not url:
        await _send_text(service, umo, f"{text}\n⚠ 未获取到播放链接")
        return {"ok": False, "reason": "no_url"}

    caps = caps_for_name(umo.split(":", 1)[0] if ":" in umo else "")
    import astrbot.api.message_components as Comp
    from astrbot.api.event import MessageChain

    if caps.vocal or caps.file:
        save_dir = await get_temp_dir()
        try:
            dl = await download_audio(
                url,
                save_dir,
                "musichub",
                int(service.config.download_timeout * 1000),
                play.get("quality") or "",
                source,
            )
        except Exception as e:  # noqa: BLE001 - 下载失败退直链文本
            logger.warning(f"{TAG} 远程投递下载失败（{e}），退直链文本")
            await _send_text(service, umo, f"{text}\n{url}")
            return {"ok": False, "reason": "download_fail"}
        path = dl["filePath"]
        keep = service.config.keep_file_sec
        _schedule_cleanup(path, keep)
        if caps.vocal:
            vocal = await prepare_vocal_file(path, direct_ext=_VOCAL_DIRECT_EXT)
            if vocal and vocal != path:
                _schedule_cleanup(vocal, keep)
            try:
                await service.context.send_message(
                    umo, MessageChain(chain=[Comp.Plain(text), Comp.Record.fromFileSystem(vocal or path)])
                )
                return {"ok": True, "reason": "voice"}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 远程投递语音失败（{e}），转文件")
        try:
            name = build_music_filename(
                singer=song.get("artist", ""),
                title=song.get("name", ""),
                ext=os.path.splitext(path)[1] or ".mp3",
            )
            await service.context.send_message(
                umo, MessageChain(chain=[Comp.Plain(text), Comp.File(name, file=path)])
            )
            return {"ok": True, "reason": "file"}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 远程投递文件失败（{e}），退文本")
            await _send_text(service, umo, f"{text}\n{url}")
            return {"ok": False, "reason": "text"}
    await _send_text(service, umo, f"{text}\n{url}")
    return {"ok": False, "reason": "text"}


async def _send_text(service, umo: str, text: str) -> None:
    try:
        from astrbot.api.event import MessageChain

        await service.context.send_message(umo, MessageChain().message(text))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{TAG} 远程投递文本失败（{umo}）：{e}")
