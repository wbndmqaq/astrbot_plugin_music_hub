"""媒体处理工具：ffmpeg 压缩、临时文件生命周期、文件名/扩展名、流式下载。"""

from __future__ import annotations

import asyncio
import functools
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import aiohttp

from . import PLUGIN_NAME, SOURCE_KG, SOURCE_NCM, SOURCE_QQ

TAG = "[music_hub]"

# ffmpeg 专用执行器（见 prepare_vocal_file 内注释）
_FFMPEG_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mh-ffmpeg")

_DEFAULT_DOWNLOAD_TIMEOUT_MS = 120000
_DEFAULT_KEEP_FILE_SEC = 60
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_DOWNLOAD_FLUSH_BYTES = 1024 * 1024
_MIN_VALID_BYTES = 256
_MAX_DOWNLOAD_BYTES = 1 << 30
_LARGE_AUDIO_BYTES = 16 * 1024 * 1024
_MEDIUM_AUDIO_BYTES = 8 * 1024 * 1024
_BITRATE_SMALL = "128k"
_BITRATE_MEDIUM = "96k"
_BITRATE_LARGE = "64k"
_BITRATE_LOW_QUALITY = "32k"
_VOCAL_MAX_BYTES = 5 * 1024 * 1024
VOCAL_DIRECT_EXT = {"mp3", "silk", "wav", "amr", "m4a", "ogg", "flac"}
_MIN_KEEP_SEC = 5

# qq_official 大文件分片上传阈值（AstrBot ≥4.27.3 适配器 >10MB 自动分片）
QQ_CHUNKED_UPLOAD_THRESHOLD = 10 * 1024 * 1024

# ──────────── ffmpeg ────────────
_ffmpeg_checked = False
_ffmpeg_ok = False


def _ffmpeg_available() -> bool:
    global _ffmpeg_checked, _ffmpeg_ok
    if not _ffmpeg_checked:
        _ffmpeg_checked = True
        _ffmpeg_ok = shutil.which("ffmpeg") is not None
    return _ffmpeg_ok


# 产物复用有效期：超过就重压，避免把坏文件（ffmpeg 异常退出的 0 字节产物）永久复用
_ARTIFACT_TTL_SEC = 300


def _reusable_artifact(path: str) -> bool:
    """已有产物是否可复用：体积达标且未过期。"""
    try:
        if os.path.getsize(path) <= _MIN_VALID_BYTES:
            return False
        return (time.time() - os.path.getmtime(path)) < _ARTIFACT_TTL_SEC
    except OSError:
        return False


def _resolve_bitrate(size: int, configured: str | None) -> str:
    """目标码率：用户配置为基准，体积越大越降一档。

    configured 是 kbps 数字的字符串（如 "128"），None/非法时回落到内置阶梯。
    """
    if configured:
        try:
            want = int(str(configured).strip().rstrip("kK"))
        except (TypeError, ValueError):
            want = 0
        if want >= 32:
            base = max(32, min(320, want))
            if size > _LARGE_AUDIO_BYTES:
                return f"{max(32, base // 2)}k"
            if size > _MEDIUM_AUDIO_BYTES:
                return f"{max(32, (base * 3) // 4)}k"
            return f"{base}k"
    if size > _LARGE_AUDIO_BYTES:
        return _BITRATE_LARGE
    if size > _MEDIUM_AUDIO_BYTES:
        return _BITRATE_MEDIUM
    return _BITRATE_SMALL


async def prepare_vocal_file(
    file_path: str,
    *,
    direct_ext: set[str] | None = None,
    max_bytes: int = _VOCAL_MAX_BYTES,
    low_quality: bool = False,
    bitrate: str | None = None,
) -> str:
    """高音质音频压成紧凑 mp3（语音/OneBot 群文件用），失败回退原文件。

    ``bitrate`` 是用户配置 ``compressBitrate``（kbps 数字）折算出的目标码率，
    为 None 时用内置阶梯。体积越大越降一档，避免大文件压完仍超限。
    """
    if not file_path:
        return file_path
    # exists/getsize 是磁盘 IO：网络盘或容器挂载上可阻塞数百 ms，不能跑在事件循环上
    # （理由同 download_audio 尾部注释）
    if not await asyncio.to_thread(os.path.exists, file_path):
        return file_path
    abs_path = os.path.abspath(file_path)
    try:
        size = await asyncio.to_thread(os.path.getsize, abs_path)
    except OSError:
        return file_path
    ext = os.path.splitext(abs_path)[1].lstrip(".").lower()
    direct = direct_ext if direct_ext is not None else VOCAL_DIRECT_EXT
    if not low_quality and ext in direct and size <= max_bytes:
        return abs_path
    if not await asyncio.to_thread(_ffmpeg_available):
        return file_path

    target = _resolve_bitrate(size, bitrate)
    stem = os.path.splitext(os.path.basename(abs_path))[0]
    # 文件名带上目标码率：用户改配置后旧产物不能被当成命中复用
    suffix = "_vocal_low" if low_quality else f"_vocal_{target}"
    out = os.path.join(os.path.dirname(abs_path), f"{stem}{suffix}.mp3")
    if await asyncio.to_thread(_reusable_artifact, out):
        return out
    # 先写临时文件再原子替换：并发压同一首（点歌台+手动）时两个 ffmpeg 不会互踩半成品。
    # .part 放源文件同目录是安全的：本函数的全部调用方（delivery._fetch_audio / remote）
    # 输入路径都来自 download_audio 落在 get_temp_dir() 的产物，startup_sweep 能兜底清理
    # 孤儿 .part —— 没有 temp 之外的输入路径，故不必改写到别处
    tmp = f"{out}.{os.urandom(3).hex()}.part"

    common = ["ffmpeg", "-y", "-i", abs_path, "-vn", "-acodec", "libmp3lame"]
    args = (
        common + ["-ar", "16000", "-ac", "1", "-b:a", _BITRATE_LOW_QUALITY, tmp]
        if low_quality
        else common + ["-ar", "44100", "-ac", "2", "-b:a", target, tmp]
    )
    try:
        # 独立小线程池：单次压制可占线程 3 分钟（timeout=180），与 to_thread 共用
        # 默认执行器时，并发压制会把统计落盘/模板渲染等短 IO 任务饿在队尾
        res = await asyncio.get_running_loop().run_in_executor(
            _FFMPEG_POOL,
            functools.partial(
                subprocess.run, args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180
            ),
        )
        if res.returncode != 0:
            await asyncio.to_thread(_remove_file, tmp)
            return file_path
        # ffmpeg 之后的校验/落盘/清理同样是磁盘 IO：延续上面的 to_thread 纪律，
        # 不裸跑在事件循环上（网络盘/容器挂载可阻塞数百 ms 卡住整个插件）
        if not await asyncio.to_thread(
            lambda: os.path.exists(tmp) and os.path.getsize(tmp) > _MIN_VALID_BYTES
        ):
            await asyncio.to_thread(_remove_file, tmp)
            return file_path
        await asyncio.to_thread(os.replace, tmp, out)
        return out
    except Exception:  # noqa: BLE001
        await asyncio.to_thread(_remove_file, tmp)
        return file_path


# ──────────── 临时目录与清理 ────────────
_temp_dir: str = ""


async def get_temp_dir() -> str:
    global _temp_dir
    if _temp_dir:
        return _temp_dir

    def _resolve() -> str:
        from astrbot.api.star import StarTools

        d = StarTools.get_data_dir(PLUGIN_NAME) / "temp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    _temp_dir = await asyncio.to_thread(_resolve)
    return _temp_dir


_cleanup_timers: dict = {}
# 已被清理定时器触发、但删除动作还在线程池里跑的任务。
# 事件循环在 to_thread 完成前关闭会导致删除丢失（temp 残留），
# cancel_cleanup_timers 需要等它们落地。
_pending_deletes: set = set()


def _remove_file(path: str) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:  # noqa: BLE001
        pass


def schedule_cleanup(file_path: str, keep_sec: int):
    try:
        delay = max(_MIN_KEEP_SEC, int(keep_sec))
    except (TypeError, ValueError):
        delay = _DEFAULT_KEEP_FILE_SEC
    loop = asyncio.get_running_loop()

    def _rm():
        _cleanup_timers.pop(handle, None)
        try:
            # 文件删除走线程池：call_later 回调同样运行在事件循环上
            task = asyncio.get_running_loop().create_task(asyncio.to_thread(_remove_file, file_path))
        except RuntimeError:  # 事件循环已停（进程退出收尾），直接删
            _remove_file(file_path)
            return
        _pending_deletes.add(task)
        task.add_done_callback(_pending_deletes.discard)

    handle = loop.call_later(delay, _rm)
    _cleanup_timers[handle] = (file_path, time.monotonic())
    return handle


async def cancel_cleanup_timers() -> None:
    """终止前清理：取消未到期定时器并删除所有已登记的临时文件（删除在线程池）。

    不设「刚注册就跳过」的宽限期——跳过会让关机前最后几秒登记的文件既不删也不注销，
    而 call_later 句柄随事件循环消亡，之后再无人回收。
    """
    to_delete: list[str] = []
    for handle, (path, _registered_at) in list(_cleanup_timers.items()):
        try:
            handle.cancel()
        except Exception:  # noqa: BLE001
            pass
        to_delete.append(path)
        _cleanup_timers.pop(handle, None)
    # 等已触发但仍在线程池里删的文件真正删完，否则循环一停就永久残留
    if _pending_deletes:
        await asyncio.gather(*list(_pending_deletes), return_exceptions=True)
    if to_delete:
        await asyncio.to_thread(lambda: [_remove_file(p) for p in to_delete])


async def startup_sweep() -> None:
    """启动清理 temp 目录里超过 1 小时的孤儿文件。"""
    try:
        d = await get_temp_dir()
        now = time.time()

        def _sweep():
            for name in os.listdir(d):
                p = os.path.join(d, name)
                try:
                    # abs() 防时钟回拨：NTP 校正后 mtime 可能是未来时间，
                    # now - mtime 为负会让条件恒假、temp 目录永久增长
                    if os.path.isfile(p) and abs(now - os.path.getmtime(p)) > 3600:
                        os.remove(p)
                except OSError:
                    pass

        await asyncio.to_thread(_sweep)
    except Exception:  # noqa: BLE001
        pass


# ──────────── 文件名 / 扩展名 ────────────
def clean_track_text(s: str, max_len: int = 40) -> str:
    if not s:
        return ""
    s = str(s).replace("【", "(").replace("】", ")").replace("《", "(").replace("》", ")")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    return re.sub(r'[\\/:*?"<>|]', "", s).strip()


def build_music_filename(*, singer: str, title: str, ext: str = "") -> str:
    s = clean_track_text(singer, 30)
    t = clean_track_text(title, 40)
    base = f"{s}-{t}" if (s and t) else (s or t or "MusicHub")
    return f"{base}{ext}"


def _ext_for_quality(quality_hint: str, url: str, ext_hint: str = "") -> str:
    if ext_hint and ext_hint.startswith("."):
        return ext_hint
    q = (quality_hint or "").lower()
    if q == "video":
        return ".mp4"
    # 杜比全景声是 mp4 容器（上游 SongFileType.ATMOS_DB=("D004",".mp4")；ncm 的 dolby 同为
    # mp4 容器），不是 flac；atmos（ATMOS_51/ATMOS_2）与 master 才是 flac 容器
    if q in ("atmos_db", "dolby"):
        return ".mp4"
    if q in (
        "flac",
        "hires",
        "high",
        "master",
        "atmos",
        "atmos_master",
        "jymaster",
        "sky",
        "viper_tape",
        "viper_clear",
        "super",
    ):
        return ".flac"
    if q in ("m4a",):
        return ".m4a"
    u = (url or "").lower()
    for ext in (".ape", ".ogg", ".flac", ".m4a", ".mp3", ".mp4"):
        if ext in u:
            return ext
    if re.search(r"f000|rs01|q000|ai00", u):
        return ".flac"
    if re.search(r"mgg|og$", u):
        return ".ogg"
    if re.search(r"c400|m4a", u):
        return ".m4a"
    # RS02 是 QQ 试听档前缀（SpecialSongFileType.TRY=("RS02",".mp3")），归 mp3 不归 flac
    if re.search(r"m800|m500|rs02|mp3", u):
        return ".mp3"
    return ".mp3"


# ──────────── 下载 ────────────
def _append_bytes(path: str, data: bytes):
    with open(path, "ab") as f:
        f.write(data)


async def download_audio(
    url: str,
    save_dir: str,
    filename: str = "musichub",
    timeout_ms: int = _DEFAULT_DOWNLOAD_TIMEOUT_MS,
    quality_hint: str = "",
    source: str = "",
) -> dict:
    from .api.http import USER_AGENT, get_session

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive",
    }
    headers.update(_SOURCE_HEADERS.get(source, {}))
    ext = _ext_for_quality(quality_hint, url)
    safe_name = re.sub(r"[^\w.-]", "", filename) or "musichub"
    file_path = os.path.join(save_dir, f"{safe_name}_{int(time.time() * 1000)}_{os.urandom(4).hex()}{ext}")

    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    size = 0
    # 目标名带时间戳 + 随机后缀基本不会已存在，但清理动作仍是磁盘 IO，
    # 不能跑在事件循环上（网络盘/容器挂载可阻塞数百 ms 卡住整个插件）
    await asyncio.to_thread(_remove_file, file_path)
    try:
        sess = get_session()
        async with sess.get(url, headers=headers, timeout=timeout, allow_redirects=True) as res:
            if res.status >= 400:
                raise RuntimeError(f"下载失败 HTTP {res.status}")
            pending = bytearray()
            first_chunk = True
            async for chunk in res.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                if first_chunk:
                    head = chunk[:32].decode("utf-8", errors="ignore").lower()
                    if "<html" in head or "<!doctype" in head:
                        raise RuntimeError("下载内容为 HTML，音频链接已失效")
                    first_chunk = False
                size += len(chunk)
                if size > _MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("下载体积超过 1GB 硬上限，已中止")
                pending.extend(chunk)
                if len(pending) >= _DOWNLOAD_FLUSH_BYTES:
                    await asyncio.to_thread(_append_bytes, file_path, bytes(pending))
                    pending.clear()
            if pending:
                await asyncio.to_thread(_append_bytes, file_path, bytes(pending))
            if size < _MIN_VALID_BYTES:
                raise RuntimeError("下载内容过小，可能是无效链接")
    except (Exception, asyncio.CancelledError):
        # os.path.exists/os.remove 在网络盘或容器挂载上可阻塞数百 ms，
        # 放在事件循环里会卡住整个插件。CancelledError 显式列出是对的
        # （3.8+ 起它继承 BaseException，不被 except Exception 捕获）
        await asyncio.to_thread(_remove_file, file_path)
        raise
    return {"filePath": file_path, "size": size}


# 各音源下载头
_SOURCE_HEADERS = {
    SOURCE_NCM: {"Referer": "https://music.163.com/", "Origin": "https://music.163.com"},
    SOURCE_KG: {"Referer": "https://www.kugou.com/"},
    SOURCE_QQ: {"Referer": "https://y.qq.com/", "Origin": "https://y.qq.com"},
}
