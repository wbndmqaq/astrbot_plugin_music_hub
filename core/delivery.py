"""音频投递编排：文案 → 原生音乐卡 → 下载 → 语音/文件双通道 → 清理登记。

三音源统一：下载 Referer/UA 按音源切换；平台差异走 core.platform_caps 能力矩阵；
OneBot 细节见 core.onebot，媒体工具见 core.media。
"""

from __future__ import annotations

import os
import re

from astrbot.api import logger
from astrbot.api.message_components import File, Record, Video

from . import SOURCE_NAMES
from .media import (  # noqa: F401 - cleanup/sweep 经 delivery 再导出（service 引用）
    _QQ_CHUNKED_UPLOAD_THRESHOLD,
    _VOCAL_DIRECT_EXT,
    _clean_track_text,
    _schedule_cleanup,
    build_music_filename,
    cancel_cleanup_timers,
    download_audio,
    get_temp_dir,
    prepare_vocal_file,
    reset_temp_dir_cache,
    startup_sweep,
)
from .onebot import _aiocq_send_file, _aiocq_send_record, send_native_music_card
from .platform_caps import caps_of

TAG = "[music_hub]"


# ──────────── 平台判定（能力矩阵门面） ────────────
def _caps(event):
    return caps_of(event)


def _is_passive_limited(event) -> bool:
    return _caps(event).passive_limited


def _no_vocal(event) -> bool:
    return not _caps(event).vocal


def _is_onebot(event) -> bool:
    return _caps(event).native_card


# ──────────── 主投递 ────────────
async def deliver_song(service, event, song: dict, play: dict, *, options: dict | None = None) -> dict:
    """交付一首歌。service: core.service.MusicService。返回 {ok, reason?, downloaded?}。"""
    options = options or {}
    cfg = service.config
    source = song.get("source", "")

    title = song.get("name") or "未知歌曲"
    singer = song.get("artist") or "未知歌手"
    quality_label = play.get("qualityLabel") or play.get("quality") or ""

    caps = _caps(event)
    is_limited = caps.passive_limited and cfg.qq_official_adapt

    # 文案：普通平台立即发；受限平台延后与媒体合并省配额
    pending_text = ""
    if cfg.send_text_info and not options.get("skipTextInfo"):
        lines = [
            f"{cfg.identify_prefix}{SOURCE_NAMES.get(source, source)}",
            f"♪ {title} - {singer}",
            f"专辑：{song['album']}" if song.get("album") else "",
            f"音质：{quality_label}" if quality_label else "",
            "" if play.get("url") else "⚠ 未获取到播放链接",
        ]
        pending_text = "\n".join(x for x in lines if x)
        if not is_limited:
            await service.send_chain(event, service.plain(pending_text))
            pending_text = ""

    # 原生音乐卡
    if cfg.send_native_card and not is_limited and not options.get("skipNativeCard") and _is_onebot(event):
        if play.get("url") or song.get("sid") or song.get("sid2"):
            await send_native_music_card(event, source, song)

    if not play.get("url"):
        if pending_text:
            await service.send_chain(event, service.plain(pending_text))
            pending_text = ""
        return {"ok": False, "reason": "no_url"}

    want_vocal = cfg.send_vocal
    want_file = cfg.upload_file
    if not want_vocal and not want_file:
        if pending_text:
            await service.send_chain(event, service.plain(pending_text))
            pending_text = ""
        return {"ok": False, "reason": "no_channel", "downloaded": False}

    # 下载（首链失败刷新一次）
    save_dir = await get_temp_dir()
    local_path = ""
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            dl = await download_audio(
                play["url"],
                save_dir,
                "musichub",
                int(cfg.download_timeout * 1000),
                play.get("quality") or "",
                source,
            )
            local_path = dl["filePath"]
            break
        except Exception as err:  # noqa: BLE001
            last_err = err
            if attempt == 0 and play.get("refetch"):
                try:
                    fresh = await play["refetch"]()
                    if fresh and fresh.get("url") and fresh["url"] != play["url"]:
                        play["url"] = fresh["url"]
                except Exception:  # noqa: BLE001
                    pass
    if not local_path:
        await service.send_chain(event, service.plain(f"下载音频失败：{last_err}\n可稍后重试，或换一首歌"))
        return {"ok": False, "reason": "download_fail", "error": str(last_err)}

    # 压缩产物
    keep_sec = cfg.keep_file_sec
    ext = os.path.splitext(local_path)[1] or ".mp3"
    file_display = build_music_filename(singer=singer, title=title, ext=ext)
    need_compress = (want_vocal and not _no_vocal(event)) or (want_file and _is_onebot(event))
    vocal_path = ""
    if need_compress and local_path:
        direct_ext = caps.notes.get("direct_ext") if is_limited else None
        vocal_path = await prepare_vocal_file(
            local_path,
            direct_ext=direct_ext or _VOCAL_DIRECT_EXT,
            low_quality=want_vocal and cfg.disable_high_quality_vocal,
        )
        if vocal_path and vocal_path != local_path:
            _schedule_cleanup(vocal_path, keep_sec)

    # 双通道
    sent_media = False
    if want_vocal:
        if _no_vocal(event):
            if not want_file:
                await service.send_chain(event, service.plain("当前平台不支持语音，且未开启文件发送"))
        else:
            voice_source = vocal_path or local_path
            ok = False
            if _is_onebot(event):
                ok, reason = await _aiocq_send_record(event, pending_text, voice_source)
                if ok:
                    sent_media = True
                    pending_text = ""
                else:
                    logger.warning(f"{TAG} OneBot 语音直发失败（{reason}），退回 Record 组件")
            if not ok and not sent_media:
                try:
                    caption = service.plain(pending_text) if pending_text else None
                    await service.send_chain(event, Record.fromFileSystem(voice_source), caption)
                    sent_media = True
                    pending_text = ""
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"{TAG} 语音发送失败: {e}")

    if want_file:
        has_compressed = bool(vocal_path and vocal_path != local_path)
        comp_display = (
            file_display.rsplit(".", 1)[0] + "_压缩版.mp3" if "." in file_display else "音乐_压缩版.mp3"
        )
        qq_large_file = False
        if is_limited:
            try:
                qq_large_file = os.path.getsize(local_path) > _QQ_CHUNKED_UPLOAD_THRESHOLD
            except OSError:
                qq_large_file = False
        qq_chunk = is_limited and cfg.qq_official_chunked and qq_large_file and _chunked_available()
        use_compressed = has_compressed and (
            _is_onebot(event) or (is_limited and qq_large_file and not qq_chunk)
        )
        file_sent = False
        if _is_onebot(event):
            try:
                await _aiocq_send_file(
                    event,
                    pending_text,
                    comp_display if has_compressed else file_display,
                    vocal_path or local_path,
                )
                file_sent = True
                pending_text = ""
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} OneBot 文件直发失败: {e}")
        if not file_sent:
            try:
                caption = service.plain(pending_text) if pending_text else None
                if use_compressed:
                    await service.send_chain(event, File(comp_display, file=vocal_path), caption)
                else:
                    await service.send_chain(event, File(file_display, file=local_path), caption)
                file_sent = True
                pending_text = ""
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 文件发送失败: {e}")
                if not use_compressed and has_compressed and cfg.ffmpeg_compress:
                    try:
                        await service.send_chain(event, File(comp_display, file=vocal_path), caption)
                        file_sent = True
                        pending_text = ""
                    except Exception as e2:  # noqa: BLE001
                        logger.warning(f"{TAG} 压缩版文件重试仍失败: {e2}")
        sent_media = sent_media or file_sent

    _schedule_cleanup(local_path, keep_sec)

    if pending_text:
        await service.send_chain(event, service.plain(pending_text))
        pending_text = ""

    if not sent_media:
        return {"ok": False, "reason": "send_fail", "downloaded": True}
    return {"ok": True, "downloaded": True}


def _chunked_available() -> bool:
    try:
        from astrbot import __version__ as _ver

        nums = re.findall(r"\d+", str(_ver).lstrip("v"))
        if len(nums) < 3:
            return False
        return [int(x) for x in nums[:3]] >= [4, 27, 3]
    except Exception:  # noqa: BLE001
        return False


async def deliver_video(
    service, event, mv: dict, url: str, *, download: bool = False, extra: list | None = None
) -> dict:
    """发送 MV：URL 直发 → 落盘发文件 → aiocqhttp base64 → URL 文本兜底。"""
    title = mv.get("name") or mv.get("songName") or "MV"
    extra = list(extra or [])
    cfg = service.config
    keep_sec = cfg.keep_file_sec
    passive_limited = _is_passive_limited(event) and cfg.qq_official_adapt
    file_name = _clean_track_text(title, 30) + ".mp4"

    async def _download() -> str:
        save_dir = await get_temp_dir()
        dl = await download_audio(
            url, save_dir, "mv_" + _clean_track_text(title, 30), int(cfg.download_timeout * 1000), "video"
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

    if local_path:
        sent = False
        try:
            await service.send_chain(event, *extra, File(file_name, file=local_path))
            sent = True
        except Exception as err:  # noqa: BLE001
            logger.warning(f"{TAG} MV 文件发送失败: {err}")
            if _is_onebot(event):
                try:
                    await _aiocq_send_file(event, "", file_name, local_path)
                    sent = True
                except Exception as err2:  # noqa: BLE001
                    logger.warning(f"{TAG} OneBot MV 直发失败: {err2}")
            else:
                try:
                    await service.send_chain(event, *extra, Video.fromFileSystem(local_path))
                    sent = True
                except Exception as err2:  # noqa: BLE001
                    logger.warning(f"{TAG} MV 视频组件发送失败: {err2}")
        if sent:
            _schedule_cleanup(local_path, keep_sec)
            return {"ok": True, "reason": "file", "url": url, "filePath": local_path}
        _schedule_cleanup(local_path, 10)

    return {"ok": False, "reason": "send_fail", "url": url, "filePath": local_path}
