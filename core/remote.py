"""远程投递：把歌从 WebUI / 点歌台队列 / 订阅推送送到指定会话（无 event 对象场景）。

发送通道走 ``context.send_message(umo, MessageChain)``：语音优先（按平台能力矩阵），
失败转群文件，再失败退文本+直链。主动消息支持矩阵见 core.platform_caps。
"""

from __future__ import annotations

import asyncio
import os

from astrbot.api import logger

from . import SOURCE_NAMES
from .media import (
    VOCAL_DIRECT_EXT,
    build_music_filename,
    download_audio,
    get_temp_dir,
    prepare_vocal_file,
    schedule_cleanup,
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
        await send_text(service, umo, f"{text}\n⚠ 未获取到播放链接")
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
            sent = await send_text(service, umo, f"{text}\n{url}")
            return _text_fallback_result(sent, "download_fail")
        path = dl["filePath"]
        keep = service.config.keep_file_sec
        vocal = ""
        try:
            if caps.vocal:
                # 与 delivery 主通道同口径：用户的压缩配置（降质/码率）在远程
                # 投递通道同样生效，此前被忽略导致各通道行为不一致。
                # 受限平台（qq_official 等）按平台 direct_ext 收窄，否则 m4a/ogg
                # 原扩展名直发 Record 会被拒收，而事件路径会正确压制成 mp3
                vocal = await prepare_vocal_file(
                    path,
                    direct_ext=(
                        caps.notes.get("direct_ext")
                        if (caps.passive_limited and service.config.qq_official_adapt)
                        else None
                    )
                    or VOCAL_DIRECT_EXT,
                    low_quality=service.config.disable_high_quality_vocal,
                    bitrate=str(service.config.compress_bitrate),
                )
                try:
                    # send_message 查无平台时返回 False 而不抛错（框架契约）：
                    # False 也按发送失败处理，否则 umo 失效后点歌台会假成功继续播下一首
                    sent = await service.context.send_message(
                        umo, MessageChain(chain=[Comp.Plain(text), Comp.Record.fromFileSystem(vocal or path)])
                    )
                    if sent is not False:
                        return {"ok": True, "reason": "voice"}
                    logger.warning(f"{TAG} 远程投递语音未送达（平台 {umo.split(':', 1)[0]} 不可用），转文件")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"{TAG} 远程投递语音失败（{e}），转文件")
            try:
                # satori 等声明不支持文件的平台（caps.file=False）：语音失败后直接退文本，
                # 白试一次 File 只会多一次注定失败的发送与等待（与 delivery 的 caps.file 门控一致）
                if caps.file:
                    name = build_music_filename(
                        singer=song.get("artist", ""),
                        title=song.get("name", ""),
                        ext=os.path.splitext(path)[1] or ".mp3",
                    )
                    sent = await service.context.send_message(
                        umo, MessageChain(chain=[Comp.Plain(text), Comp.File(name, file=path)])
                    )
                    if sent is not False:
                        return {"ok": True, "reason": "file"}
                    logger.warning(f"{TAG} 远程投递文件未送达（平台 {umo.split(':', 1)[0]} 不可用），退文本")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 远程投递文件失败（{e}），退文本")
            sent = await send_text(service, umo, f"{text}\n{url}")
            return _text_fallback_result(sent, "send_fail")
        finally:
            # 下载/压缩/发送全部结束（成功、失败、中途取消）才开始计时：
            # ffmpeg 压制（超时 180s）与慢上行都可能超过 keep_file_sec，
            # 先登记会把还没发出去的文件删掉——与 delivery 的计时纪律一致，
            # finally 保证取消/异常路径同样不漏登记防孤儿
            schedule_cleanup(path, keep)
            if vocal and vocal != path:
                schedule_cleanup(vocal, keep)
    sent = await send_text(service, umo, f"{text}\n{url}")
    return _text_fallback_result(sent, "send_fail")


def _text_fallback_result(sent: bool, fail_reason: str) -> dict:
    """文本兜底的结果：用户实际收到了直链内容就按成功记——

    点歌台 _player 见 ok=False 会追发「发送失败」、统计也记失败，
    但直链文本是用户确实可用的内容，不算失败。
    """
    return {"ok": sent, "reason": "text_fallback" if sent else fail_reason}


async def send_text(service, umo: str, text: str) -> bool:
    """无事件向指定会话发文本。send_message 查无平台时返回 False 而不抛错
    （框架契约），这里把 False 同样按失败计——调用方据它决定降级与记账。

    超时兜底 60s（对齐 onebot 直发通道）：send_message 挂死（平台假死）时
    不加超时会卡住订阅推送整轮与点歌台的投递链。"""
    try:
        from astrbot.api.event import MessageChain

        sent = await asyncio.wait_for(
            service.context.send_message(umo, MessageChain().message(text)), timeout=60
        )
        return sent is not False
    except asyncio.TimeoutError:
        logger.warning(f"{TAG} 远程投递文本超时（{umo}），按失败处理")
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{TAG} 远程投递文本失败（{umo}）：{e}")
        return False
