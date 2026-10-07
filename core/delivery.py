"""音频投递编排：文案 → 原生音乐卡 → 下载 → 语音/文件双通道 → 清理登记。

三音源统一：下载 Referer/UA 按音源切换；平台差异走 core.platform_caps 能力矩阵；
OneBot 细节见 core.onebot，媒体工具见 core.media。

deliver_song 只做编排，取流/投递/压缩/清理各自独立成函数，
便于单测时替换其中任一环节（原先 180 行单函数无法替换任何一步）。
"""

from __future__ import annotations

import os
import re

from astrbot.api import logger
from astrbot.api.message_components import File, Record, Video

from . import SOURCE_NAMES
from .media import (
    QQ_CHUNKED_UPLOAD_THRESHOLD,
    VOCAL_DIRECT_EXT,
    build_music_filename,
    clean_track_text,
    download_audio,
    get_temp_dir,
    prepare_vocal_file,
    schedule_cleanup,
)
from .onebot import aiocq_send_file, aiocq_send_record, send_native_music_card
from .platform_caps import caps_of

TAG = "[music_hub]"


class _Pending:
    """待发文案。

    受限平台（qq_official）要把文案延后与媒体合并发送以省配额，
    每次成功发出媒体后清空，避免同一段文案发两遍。
    """

    __slots__ = ("text",)

    def __init__(self, text: str = ""):
        self.text = text

    def take(self) -> str:
        out, self.text = self.text, ""
        return out

    async def flush(self, service, event) -> None:
        if self.text:
            await service.send_chain(event, service.plain(self.text))
            self.text = ""


async def _fetch_audio(service, song: dict, play: dict) -> tuple[str, Exception | None]:
    """下载音频；首链失败刷新一次 URL 重试（直链签名通常会过期）。"""
    cfg = service.config
    save_dir = await get_temp_dir()
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            dl = await download_audio(
                play["url"],
                save_dir,
                "musichub",
                int(cfg.download_timeout * 1000),
                play.get("quality") or "",
                song.get("source", ""),
            )
            return dl["filePath"], None
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt:
                break
            # 不走 play 里的闭包：那会让 play 携带函数对象，无法安全跨 JSON 边界。
            try:
                fresh = await service.resolve_play(song)
            except Exception:  # noqa: BLE001
                break
            new_url = fresh.get("url") or ""
            if not new_url or new_url == play["url"]:
                break
            play["url"] = new_url
            play["quality"] = fresh.get("quality") or play.get("quality")
    return "", last_err


async def _send_voice(service, event, caps, *, path: str, pending: _Pending) -> bool:
    """语音通道：OneBot 直发优先，失败退回 Record 组件。"""
    if caps.native_card:
        ok, reason = await aiocq_send_record(event, pending.text, path)
        if ok:
            pending.take()  # 直发成功（文案已随媒体发出）才清空；失败退回组件通道还要用
            return True
        logger.warning(f"{TAG} OneBot 语音直发失败（{reason}），退回 Record 组件")
    try:
        caption = service.plain(pending.take()) if pending.text else None
        await service.send_chain(event, Record.fromFileSystem(path), caption)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{TAG} 语音发送失败: {e}")
        return False


async def _send_file(
    service, event, caps, *, local_path: str, vocal_path: str, display: str, pending: _Pending
) -> bool:
    """文件通道：OneBot 直发 → File 组件 → 压缩版重试。"""
    cfg = service.config
    has_compressed = bool(vocal_path and vocal_path != local_path)
    comp_display = display.rsplit(".", 1)[0] + "_压缩版.mp3" if "." in display else "音乐_压缩版.mp3"
    is_limited = caps.passive_limited and cfg.qq_official_adapt
    qq_large_file = False
    if is_limited:
        try:
            qq_large_file = os.path.getsize(local_path) > QQ_CHUNKED_UPLOAD_THRESHOLD
        except OSError:
            qq_large_file = False
    qq_chunk = is_limited and cfg.qq_official_chunked and qq_large_file and _chunked_available()
    use_compressed = has_compressed and (caps.native_card or (is_limited and qq_large_file and not qq_chunk))

    if caps.native_card:
        try:
            await aiocq_send_file(
                event, pending.take(), comp_display if has_compressed else display, vocal_path or local_path
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} OneBot 文件直发失败: {e}")

    caption = service.plain(pending.take()) if pending.text else None
    try:
        target = vocal_path if use_compressed else local_path
        name = comp_display if use_compressed else display
        await service.send_chain(event, File(name, file=target), caption)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{TAG} 文件发送失败: {e}")

    if not use_compressed and has_compressed and cfg.ffmpeg_compress:
        try:
            await service.send_chain(event, File(comp_display, file=vocal_path), caption)
            return True
        except Exception as e2:  # noqa: BLE001
            logger.warning(f"{TAG} 压缩版文件重试仍失败: {e2}")
    return False


def _chunked_available() -> bool:
    try:
        from astrbot import __version__ as _ver

        nums = re.findall(r"\d+", str(_ver).lstrip("v"))
        if len(nums) < 3:
            return False
        return [int(x) for x in nums[:3]] >= [4, 27, 3]
    except Exception:  # noqa: BLE001
        return False


async def deliver_song(service, event, song: dict, play: dict, *, options: dict | None = None) -> dict:
    """交付一首歌。service: core.service.MusicService。返回 {ok, reason?, downloaded?}。"""
    options = options or {}
    cfg = service.config
    source = song.get("source", "")
    title = song.get("name") or "未知歌曲"
    singer = song.get("artist") or "未知歌手"
    quality_label = play.get("qualityLabel") or play.get("quality") or ""

    caps = caps_of(event)
    is_limited = caps.passive_limited and cfg.qq_official_adapt

    lines = [
        f"{cfg.identify_prefix}{SOURCE_NAMES.get(source, source)}",
        f"♪ {title} - {singer}",
        f"专辑：{song['album']}" if song.get("album") else "",
        f"音质：{quality_label}" if quality_label else "",
        "" if play.get("url") else "⚠ 未获取到播放链接",
    ]
    want_text = cfg.send_text_info and not options.get("skipTextInfo")
    # 受限平台延后发文案，与媒体合并省配额
    pending = _Pending("\n".join(x for x in lines if x) if want_text else "")
    if pending.text and not is_limited:
        await service.send_chain(event, service.plain(pending.take()))

    if cfg.send_native_card and not is_limited and not options.get("skipNativeCard") and caps.native_card:
        if play.get("url") or song.get("sid") or song.get("sid2"):
            await send_native_music_card(event, source, song)

    if not play.get("url"):
        await pending.flush(service, event)
        return {"ok": False, "reason": "no_url"}

    # want_vocal 保持"用户想发语音"，平台支不支持在下面分支里判：
    # 不支持时要给出明确文案，而不是当成没开语音直接跳过。
    want_vocal = cfg.send_vocal
    want_file = cfg.upload_file
    vocal_possible = want_vocal and caps.vocal
    if not want_vocal and not want_file:
        await pending.flush(service, event)
        return {"ok": False, "reason": "no_channel", "downloaded": False}

    local_path, last_err = await _fetch_audio(service, song, play)
    if not local_path:
        await service.send_chain(event, service.plain(f"下载音频失败：{last_err}\n可稍后重试，或换一首歌"))
        return {"ok": False, "reason": "download_fail", "error": str(last_err)}

    # 下载一成功就登记清理：之后无论哪个 await 抛错，temp 都不会留下孤儿文件
    schedule_cleanup(local_path, cfg.keep_file_sec)

    ext = os.path.splitext(local_path)[1] or ".mp3"
    display = build_music_filename(singer=singer, title=title, ext=ext)
    vocal_path = ""
    if (vocal_possible or (want_file and caps.native_card)) and local_path:
        vocal_path = await prepare_vocal_file(
            local_path,
            direct_ext=(caps.notes.get("direct_ext") if is_limited else None) or VOCAL_DIRECT_EXT,
            low_quality=vocal_possible and cfg.disable_high_quality_vocal,
            bitrate=str(cfg.compress_bitrate),
        )
        if vocal_path and vocal_path != local_path:
            schedule_cleanup(vocal_path, cfg.keep_file_sec)

    sent_media = False
    if want_vocal:
        if not caps.vocal:
            if not want_file:
                await service.send_chain(event, service.plain("当前平台不支持语音，且未开启文件发送"))
        else:
            sent_media = await _send_voice(
                service, event, caps, path=vocal_path or local_path, pending=pending
            )
    if want_file:
        sent_media = (
            await _send_file(
                service,
                event,
                caps,
                local_path=local_path,
                vocal_path=vocal_path,
                display=display,
                pending=pending,
            )
            or sent_media
        )

    await pending.flush(service, event)
    if not sent_media:
        return {"ok": False, "reason": "send_fail", "downloaded": True}
    return {"ok": True, "downloaded": True}


async def deliver_video(
    service, event, mv: dict, url: str, *, download: bool = False, extra: list | None = None
) -> dict:
    """发送 MV：URL 直发 → 落盘发文件 → aiocqhttp base64 → URL 文本兜底。"""
    title = mv.get("name") or mv.get("songName") or "MV"
    extra = list(extra or [])
    cfg = service.config
    keep_sec = cfg.keep_file_sec
    caps = caps_of(event)
    passive_limited = caps.passive_limited and cfg.qq_official_adapt
    safe_title = clean_track_text(title, 30)
    file_name = safe_title + ".mp4"

    async def _download() -> str:
        save_dir = await get_temp_dir()
        dl = await download_audio(
            url, save_dir, "mv_" + safe_title, int(cfg.download_timeout * 1000), "video"
        )
        return dl["filePath"]

    local_path = ""
    if download:
        try:
            local_path = await _download()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{TAG} MV 下载失败: {err}")

    if not local_path and not download and not passive_limited:
        try:
            await service.send_chain(event, *extra, Video.fromURL(url))
            return {"ok": True, "reason": "url", "url": url}
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{TAG} MV 直发 URL 失败，尝试落盘: {err}")
            try:
                local_path = await _download()
            except Exception as err2:  # noqa: BLE001
                logger.warning(f"{TAG} MV 落盘失败: {err2}")

    if not local_path and passive_limited and not download:
        try:
            local_path = await _download()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{TAG} MV 落盘失败: {err}")

    if not local_path:
        return {"ok": False, "reason": "send_fail", "url": url, "filePath": ""}

    # 落盘即登记：下面任何一环抛错都不会留下孤儿文件
    schedule_cleanup(local_path, keep_sec)

    try:
        await service.send_chain(event, *extra, File(file_name, file=local_path))
        return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
    except Exception as err:  # noqa: BLE001
        logger.warning(f"{TAG} MV 文件发送失败: {err}")

    if caps.native_card:
        try:
            await aiocq_send_file(event, "", file_name, local_path)
            return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
        except Exception as err2:  # noqa: BLE001
            logger.warning(f"{TAG} OneBot MV 直发失败: {err2}")
    else:
        try:
            await service.send_chain(event, *extra, Video.fromFileSystem(local_path))
            return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
        except Exception as err2:  # noqa: BLE001
            logger.warning(f"{TAG} MV 视频组件发送失败: {err2}")

    return {"ok": False, "reason": "send_fail", "url": url, "filePath": local_path}
