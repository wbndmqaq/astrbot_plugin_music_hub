"""音频投递编排：文案 → 原生音乐卡 → 下载 → 语音/文件双通道 → 清理登记。

三音源统一：下载 Referer/UA 按音源切换；平台差异走 core.platform_caps 能力矩阵；
OneBot 细节见 core.onebot，媒体工具见 core.media。

deliver_song 只做编排，取流/投递/压缩/清理各自独立成函数，
便于单测时替换其中任一环节（原先 180 行单函数无法替换任何一步）。
"""

from __future__ import annotations

import asyncio
import os
import re

from astrbot.api import logger
from astrbot.api.message_components import File, Record, Video

from . import SOURCE_NAMES
from .errors import sanitize_message
from .media import (
    QQ_CHUNKED_UPLOAD_THRESHOLD,
    VOCAL_DIRECT_EXT,
    build_music_filename,
    download_audio,
    get_temp_dir,
    prepare_vocal_file,
    safe_display_stem,
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
            # 档位展示字段同步回写：首段识别文案虽已按旧档发出改不回来，
            # 但 play 里的 label 还会被队列/统计等下游读取，留着旧档会持续失真
            if fresh.get("quality"):
                play["qualityLabel"] = fresh.get("qualityLabel") or play.get("qualityLabel")
                play["label"] = fresh.get("label") or play.get("label")
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
        # caption 留在 pending 里等发送成功才 take：失败路径（退压缩版/最终 flush）
        # 还要靠它补发说明，先 take 后发会让失败后的裸文件没有歌名说明
        caption = service.plain(pending.text) if pending.text else None
        await service.send_chain(event, Record.fromFileSystem(path), caption)
        pending.take()
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
            # getsize 是磁盘 IO，走线程池（同 media.download_audio 尾部注释的理由）
            qq_large_file = await asyncio.to_thread(os.path.getsize, local_path) > QQ_CHUNKED_UPLOAD_THRESHOLD
        except OSError:
            qq_large_file = False
    qq_chunk = is_limited and cfg.qq_official_chunked and qq_large_file and _chunked_available()
    use_compressed = has_compressed and (caps.native_card or (is_limited and qq_large_file and not qq_chunk))

    if caps.native_card:
        try:
            # 传 pending.text 只读：aiocq_send_file 抛错时文案还在，组件通道重发不丢；
            # 成功（文案已随文件发出）才 take 清空 —— 与 _send_voice 同一纪律
            await aiocq_send_file(
                event, pending.text, comp_display if has_compressed else display, vocal_path or local_path
            )
            pending.take()
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} OneBot 文件直发失败: {e}")

    # caption 同 _send_voice：发送成功才 take 清空，失败后由压缩重试或最终 flush 补发
    caption = service.plain(pending.text) if pending.text else None
    try:
        target = vocal_path if use_compressed else local_path
        name = comp_display if use_compressed else display
        await service.send_chain(event, File(name, file=target), caption)
        pending.take()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"{TAG} 文件发送失败: {e}")

    if not use_compressed and has_compressed and cfg.ffmpeg_compress:
        try:
            await service.send_chain(event, File(comp_display, file=vocal_path), caption)
            pending.take()
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


async def deliver_song(
    service, event, song: dict, play: dict, *, options: dict | None = None, quiet: bool = False
) -> dict:
    """交付一首歌。service: core.service.MusicService。返回 {ok, reason?, downloaded?}。

    quiet：失败时不发任何补发文案（受限平台的 pending 说明也不补），
    供自动换源等调用方对中间失败静音、只发最终汇总。
    """
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

    # 通道判定（用户开关 × 平台能力）必须先于原生卡与下载：两个通道都不可用时
    # （lark/dingtalk/webchat 等，见 platform_caps 矩阵），卡与整曲下载（含换源
    # 重试 + 超时窗口）都白发，用户只会等到最后一句「不支持」文案。
    # want_vocal 保持"用户想发语音"，平台支不支持在下面文案里说清楚，
    # 而不是当成没开语音直接跳过。
    want_vocal = cfg.send_vocal
    want_file = cfg.upload_file
    vocal_possible = want_vocal and caps.vocal
    file_possible = want_file and caps.file
    if not vocal_possible and not file_possible:
        # 按用户实际想发的通道措辞：只开了文件却提示「不支持语音与文件」是误导
        if want_vocal and not want_file:
            await service.send_chain(event, service.plain("当前平台不支持语音，且未开启文件发送"))
        elif want_vocal and want_file:
            await service.send_chain(event, service.plain("当前平台不支持语音与文件发送"))
        else:
            await service.send_chain(event, service.plain("当前平台不支持文件发送"))
        await pending.flush(service, event)
        return {"ok": False, "reason": "no_channel", "downloaded": False}

    if cfg.send_native_card and not is_limited and not options.get("skipNativeCard") and caps.native_card:
        if play.get("url") or song.get("sid") or song.get("sid2"):
            await send_native_music_card(event, source, song)

    if not play.get("url"):
        if not quiet:
            await pending.flush(service, event)
        return {"ok": False, "reason": "no_url"}

    local_path, last_err = await _fetch_audio(service, song, play)
    if not local_path:
        # 原文只留日志：网络层异常（aiohttp/TLS）原文含 CDN 主机名/直链，透给
        # 聊天端、经 play_group 汇总进群或经 LLM 工具回传前都要剥（同 errors
        # 的 _LEAK_RE 口径；download_audio 自包的 RuntimeError 也统一在此处理）
        err_text = sanitize_message(last_err)
        logger.warning(f"{TAG} 下载音频失败: {last_err}")
        if quiet:
            # 中间失败只记日志：文案由调用方最终汇总，否则换源 3 个版本全挂时
            # 群里每个失败版本都收到一条「下载失败」
            return {"ok": False, "reason": "download_fail", "error": err_text}
        # 受限平台的 pending 文案还没发过：先补发歌名说明再报错，别让用户只看到
        # 一句「下载失败」却不知道刚才点的是哪首
        await pending.flush(service, event)
        await service.send_chain(event, service.plain(f"下载音频失败：{err_text}\n可稍后重试，或换一首歌"))
        return {"ok": False, "reason": "download_fail", "error": err_text}

    ext = os.path.splitext(local_path)[1] or ".mp3"
    display = build_music_filename(singer=singer, title=title, ext=ext)
    vocal_path = ""
    try:
        if (vocal_possible or (want_file and caps.native_card)) and local_path:
            vocal_path = await prepare_vocal_file(
                local_path,
                direct_ext=(caps.notes.get("direct_ext") if is_limited else None) or VOCAL_DIRECT_EXT,
                low_quality=vocal_possible and cfg.disable_high_quality_vocal,
                bitrate=str(cfg.compress_bitrate),
            )
    except BaseException:
        # 清理定时器不能在压缩前登记：ffmpeg 压制（超时 180s）可能超过
        # keep_file_sec（默认 60s），会把还没发出去的音频删掉。压缩期间
        # 改用异常兜底登记防孤儿文件，正常路径在发送结束后再计时
        schedule_cleanup(local_path, cfg.keep_file_sec)
        raise

    sent_media = False
    try:
        # 平台不支持的说明文案已前移到下载前的通道预判：走到这里至少有一个
        # 真实可用通道，不再有「只发文本不白发注定失败的媒体」的分支
        if want_vocal and caps.vocal:
            sent_media = await _send_voice(
                service, event, caps, path=vocal_path or local_path, pending=pending
            )
        if want_file and caps.file:
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
    finally:
        # 发送全部结束（成功/失败/中途抛错）才开始计时：慢上行 >keep_file_sec
        # 会把正在发送的文件删掉——计时起点必须晚于文件的最后一个使用点
        schedule_cleanup(local_path, cfg.keep_file_sec)
        if vocal_path and vocal_path != local_path:
            schedule_cleanup(vocal_path, cfg.keep_file_sec)

    if not sent_media:
        # 全部通道都试过但发送失败（禁言/协议端超时等）：非 quiet 路径补一句当面
        # 说明，否则用户只看到「识别：…」，歌没到、没报错、没直链；quiet 路径
        # （连播/换源中间态）仍静默，由调用方汇总
        if not quiet:
            await service.send_chain(
                event, service.plain("发送失败：语音与文件通道均不可用（可能被禁言或协议端异常）")
            )
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
    # 显示名词干走保留名守卫：clean_track_text 产物 + ".mp4" 可以拼出 CON.mp4
    # 这类 Windows 保留名形态（协议端拒绝创建）
    safe_title = safe_display_stem(title, 30)
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
            # 下载失败不能静默（原实现直接 ok=False 且什么都不发）：
            # 先退 Video.fromURL 直发，再退直链文本，与 send_audio_to 的降级链对齐。
            # 受限平台本就走不了 URL 直发，直接落文本
            if not passive_limited:
                try:
                    await service.send_chain(event, *extra, Video.fromURL(url))
                    return {"ok": True, "reason": "url", "url": url}
                except Exception as err2:  # noqa: BLE001
                    logger.warning(f"{TAG} MV 直发 URL 失败，退直链文本: {err2}")
            await service.send_chain(event, *extra, service.plain(f"MV「{title}」：{url}"))
            # 文本兜底用户实际收到了直链，按成功记，调用方不再追发失败提示
            return {"ok": True, "reason": "text_fallback", "url": url, "filePath": ""}

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

    # caps.file=False 的平台（lark/webchat/satori）File 必发送失败：前置跳过，
    # 直接走 aiocqhttp/URL 降级，省一跳注定失败的发送与日志噪音（对齐 deliver_song）
    # 计时起点必须在发送全部结束之后：MV 动辄几十 MB，慢上行 >keep_file_sec 时
    # 提前登记的定时器会把正在上传的文件删掉（对齐 deliver_song 的 finally 纪律）
    try:
        if caps.file:
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
    finally:
        schedule_cleanup(local_path, keep_sec)
