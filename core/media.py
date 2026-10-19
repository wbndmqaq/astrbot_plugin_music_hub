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
from astrbot.api import logger

from . import PLUGIN_NAME, SOURCE_KG, SOURCE_NCM, SOURCE_QQ

TAG = "[music_hub]"

# ffmpeg 专用执行器（见 prepare_vocal_file 内注释）。惰性获取：shutdown 后置 None，
# 下次使用自动重建——「停用→启用」在同进程内只重跑 initialize、不重载本模块，
# 一次性 shutdown 会让之后所有语音压缩静默降级为直发原文件
_FFMPEG_POOL: ThreadPoolExecutor | None = None

_DEFAULT_DOWNLOAD_TIMEOUT_MS = 120000
_DEFAULT_KEEP_FILE_SEC = 60
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_DOWNLOAD_FLUSH_BYTES = 1024 * 1024
_MIN_VALID_BYTES = 256
_MAX_DOWNLOAD_BYTES = 1 << 30
_MAX_REDIRECTS = 5  # 下载重定向手动跟随的上限（每跳前都过内网预检）
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


def _get_ffmpeg_pool() -> ThreadPoolExecutor:
    """惰性获取 ffmpeg 执行器；被 shutdown 后置 None 自动重建。

    「停用→启用」在同进程内只重跑 initialize、不重载本模块：一次性 shutdown
    若不留复位路径，之后所有语音压缩会静默降级为直发原文件（与 reset_http
    修复的是同一类问题）。"""
    global _FFMPEG_POOL
    if _FFMPEG_POOL is None:
        _FFMPEG_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mh-ffmpeg")
    return _FFMPEG_POOL


def shutdown_ffmpeg_pool() -> None:
    """插件终止时关闭 ffmpeg 执行器。

    线程是模块级且非 daemon：AstrBot 重载插件会重新执行本模块，旧模块的
    执行器若不 shutdown，每次重载都永久残留 2 个空闲线程（累积性泄漏）。
    wait=False 不等在跑的压制（subprocess 无法中断，最长 180s 自行结束）。
    关闭后置 None：下次 _get_ffmpeg_pool() 重建，压缩能力不随一次 terminate 永久丢失。
    """
    global _FFMPEG_POOL
    if _FFMPEG_POOL is not None:
        _FFMPEG_POOL.shutdown(wait=False, cancel_futures=True)
        _FFMPEG_POOL = None


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
        # 复用旧产物必须立即续期清理定时器：上一次投递的 finally 已为同一路径
        # 登记了 T+keep 到期的删除，而本路径的前置下载/检查窗口（下载超时上限
        # 120s）可能长于旧定时器的剩余寿命——到点会把本次发送中/发送前的产物
        # 删掉，静默降级到文件/文本通道还触发整段重压。续期从现在起算，覆盖
        # 本轮投递全程（与 schedule_cleanup 的同路径去重续期互补：那边防
        # 「新登记撞旧定时器」，这里防「旧定时器先到期」）
        schedule_cleanup(out, 300)
        return out
    # 先写临时文件再原子替换：并发压同一首（点歌台+手动）时两个 ffmpeg 不会互踩半成品。
    # .part 放源文件同目录是安全的：本函数的全部调用方（delivery._fetch_audio / remote）
    # 输入路径都来自 download_audio 落在 get_temp_dir() 的产物，startup_sweep 能兜底清理
    # 孤儿 .part —— 没有 temp 之外的输入路径，故不必改写到别处
    tmp = f"{out}.{os.urandom(3).hex()}.part"
    # replace 是否已落成：替换完成后才被取消时，产物 out 已生成，须自行登记清理
    # （delivery 的异常兜底只登记源文件，不知道这个产物）
    replaced = False

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
            _get_ffmpeg_pool(),
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
        replaced = True
        return out
    except (Exception, asyncio.CancelledError) as e:
        # CancelledError 显式列出（3.8+ 起继承 BaseException，不被 except Exception
        # 捕获）：协程在 ffmpeg 运行期间被取消时也清掉 .part，不留孤儿到 1 小时后的
        # startup_sweep；网络盘/容器挂载上 os.remove 是磁盘 IO，照旧走线程
        await asyncio.to_thread(_remove_file, tmp)
        if isinstance(e, asyncio.CancelledError):
            if replaced:
                # 取消落在 replace 与 return 之间的窗口：产物登记清理，
                # 5 分钟留量覆盖发送方重试，避免孤儿滞留到启动清扫
                schedule_cleanup(out, 300)
            raise
        # 非 cancel 异常（ffmpeg 被杀/OSError/参数问题）静默回退原文件会让
        # 「永远拿到原始 FLAC 大文件」这类问题排查无据——与全插件「吞掉但不
        # 无声」纪律对齐，至少留一条 warning
        logger.warning(f"{TAG} 语音压缩失败，回退原文件: {type(e).__name__}: {e}")
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

    # 同路径去重续期：产物复用（_reusable_artifact）会让上一次投递登记的定时器
    # 指向本次正在发送的文件——旧定时器先到点就把新发送删了。重登记从现在起算，
    # 计时起点晚于文件的最后一个使用点
    for old, (old_path, _at) in list(_cleanup_timers.items()):
        if old_path == file_path:
            _cleanup_timers.pop(old, None)
            try:
                old.cancel()
            except Exception:  # noqa: BLE001 - 已触发的句柄 cancel 无害
                pass

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
    # 控制字符（\x00-\x1f）先进空格再随 \s+ 归一：残留进文件名会被部分
    # 文件系统/协议端拒绝，直接删除又可能把相邻词粘连。
    # 双向控制符（U+202A-202E 覆写、U+200E/200F 标记）一并剥：残留进文件名会在
    # 聊天客户端按双向算法重排显示（RTL 覆写伪装后缀段）
    s = re.sub(r"[\x00-\x1f\u202a-\u202e\u200e\u200f]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[:max_len]
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    # 挂尾的点/空格在 Windows 路径非法（系统拒绝创建，os.replace 会失败），剥掉；
    # 开头不剥——「.5 Quart」这类歌名确实存在。CON/NUL 等保留名不处理：本函数
    # 的产物都经 build_music_filename 拼接或带时间戳/随机后缀，不会是裸保留名
    return s.strip(" .")


# Windows 保留设备名：带扩展名同样非法（CON.mp4 创建会被系统拒绝），且
# 「保留名紧跟扩展名」（CON.mp3 的词干仍是 CON）也是 Microsoft 命名文档明确
# 不建议的形态——按前缀判定一并兜住，clean_track_text 只保证产物不「裸」保留名
# （拼时间戳/随机后缀），显示名直接 +".ext" 的调用方走 safe_display_stem
_WIN_RESERVED_RE = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


def safe_display_stem(s: str, max_len: int = 40) -> str:
    """显示名/落盘名共用的「词干」清洗：clean_track_text 之外再兜保留名形态。"""
    out = clean_track_text(s, max_len)
    if _WIN_RESERVED_RE.match(out):
        out = "_" + out
    return out


def build_music_filename(*, singer: str, title: str, ext: str = "") -> str:
    s = safe_display_stem(singer, 30)
    t = safe_display_stem(title, 40)
    # 词干兜保留名（safe_display_stem）：artist="CON" 且 name 为空时 base="CON"，
    # 拼 ".mp3" 即 Windows 保留名带扩展名形态（协议端建文件失败）
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


_INTERNAL_HOST_RE = re.compile(
    r"^(localhost$|127\.|10\.|0\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|\[?::1\]?$|\[::1\]$)",
    re.IGNORECASE,
)


def _is_internal_host(host: str | None) -> bool:
    """host 字面是回环/私网/链路本地地址（含 localhost、IPv6 ::1）时返回 True。

    注意只校验 host 字面、不解析 DNS：解析到私网 IP 的公网域名（DNS rebinding/
    污染场景）不在此拦截面内。威胁模型是拦 API 返回的裸 IP 直链，够用。"""
    if not host:
        return False
    h = host.strip("[]").lower()
    # IPv6 zone-id（http://[fe80::1%25eth0]/）解析出 % 后缀：ip_address() 对它
    # 抛 ValueError 且不以 0x/纯数字开头，会漏拦放行——链路本地字面量按内网拦
    if "%" in h:
        return True
    # 非规范字面量归一：全角数字/全角点经 NFKC（Chromium 的 WHATWG 解析强制做）、
    # 表意点经 IDNA（Windows getaddrinfo 实测接受）都会归一成 ASCII——
    # 「１２７．０．０．１」getaddrinfo 实测解析到 127.0.0.1，放行即成与十进制
    # 整数完全同族的字面 SSRF 旁路。归一后重走全部字面判定，不为每种形态单列规则
    import unicodedata

    norm = unicodedata.normalize("NFKC", h)
    for _dot in ("。", "｡", "．"):
        norm = norm.replace(_dot, ".")
    if norm != h:
        h = norm
    if not h:
        return False
    # 尾点 FQDN 与 *.localhost：Windows DNS client 与 Linux systemd-resolved 按
    # RFC 6761 把「localhost.」「foo.localhost」解析到 127.0.0.1（getaddrinfo 走
    # OS 解析器），而正则的 localhost$ 精确锚定不认尾点形态——剥尾点后重判，
    # 与全角/十进制整数同族的字面旁路收口
    h2 = h.rstrip(".")
    if h2 != h and h2:
        h = h2
    if h2 == "localhost" or h2.endswith(".localhost"):
        return True
    if _INTERNAL_HOST_RE.match(h):
        return True
    import ipaddress

    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # ip_address 只认点分十进制；getaddrinfo 还接受十进制整数（2130706433）、
        # 十六进制/八进制（0x7f.0.0.1、0177.0.0.1）等非规范字面量，全部解析到
        # 回环/私网——放行即成 SSRF 旁路
        if h.isdigit() and h.isascii():
            # 纯数字 host 不是合法域名（不存在全数字 TLD），getaddrinfo 会按
            # 整数 IP 解析：无论能否还原成 IPv4，一律按内网拦
            return True
        if h[0] == "0" and len(h) > 1 and (h[1] == "x" or h[1].isdigit()):
            # 0x7f… / 0177… 开头的非规范字面量：合法域名不以 0x/前导 0 起头
            return True
        # 逐段十六进制/前导零（glibc inet_aton 语义，Linux/Docker 实际解析路径）：
        # 「172.0x10.0.1」（0x10=16 → 172.16.0.1）、「172.032.0.1」（032=26 →
        # 172.26.0.1）首段是公开段，上面两道首段检查都不中——收口条件是 host
        # 仅含 [0-9a-fx.]（真实域名至少要一个其余字母的 TLD，如 .us/.com）且
        # 任一段写成 0x 十六进制或带前导零的八进制；拦的是解析方向，零误伤
        if re.fullmatch(r"[0-9a-fx.]+", h) and any(
            seg.startswith("0x") or (len(seg) > 1 and seg.startswith("0") and seg.isdigit())
            for seg in h.split(".")
        ):
            return True
        return False
    # IPv4-mapped IPv6 字面量（::ffff:127.0.0.1）在部分 Python 版本上 is_private
    # 不判私网：解析出映射的 IPv4 就按它再判一遍（ipv4_mapped 仅 IPv6Address 有）
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_private or mapped.is_loopback
    # is_private 不含 CGNAT（100.64/10，Tailscale 等 mesh 组网指向宿主机可达的
    # 虚拟接口）与组播/保留/链路本地段（224/4、240/4、fe80::/10）——都按内网拦。
    # 198.18/15（fake-ip 保留段）不另设规则：随标准库 is_private 的版本行为走
    # （3.13+ 已判私网，旧版放行），Clash TUN 部署见配置注释
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    if ip.is_multicast or ip.is_reserved or ip.is_link_local:
        return True
    return ip.is_private or ip.is_loopback


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
        # 初始 URL 先预检：只看最终 res.url 的话，直连内网的初始 URL 在 TCP
        # 连接已发出后才被拦，多级重定向「中途进内网再跳出」的链路也会漏
        from urllib.parse import urlsplit

        if _is_internal_host(urlsplit(str(url)).hostname):
            raise RuntimeError("下载地址指向内网，已中止")
        sess = get_session()
        # 重定向手动逐跳跟随：allow_redirects=True 时对每一跳都已发出完整 GET，
        # 拿到响应头才查 res.url 属于「先打后拦」，拦不住对内网的探测副作用
        dest = str(url)
        for _hop in range(_MAX_REDIRECTS):
            async with sess.get(dest, headers=headers, timeout=timeout, allow_redirects=False) as res:
                if res.status in (301, 302, 303, 307, 308):
                    loc = str(res.headers.get("Location") or "")
                    if not loc:
                        raise RuntimeError("下载重定向缺少 Location")
                    # Location 允许相对地址（RFC 7231 §7.1.2）：原样塞回 get 会抛
                    # InvalidURL；以当前响应 URL 为基解析（对齐 color.py 封面路径）
                    dest = str(res.url.join(loc))
                    if _is_internal_host(urlsplit(dest).hostname):
                        raise RuntimeError("下载重定向指向内网地址，已中止")
                    continue
                if res.status >= 400:
                    raise RuntimeError(f"下载失败 HTTP {res.status}")
                # 上游 API 返回的直链可能 302 换 CDN 域，但不应被引到内网地址
                # （与 resolve.py 短链白名单同一威胁模型；按 host 字面拦截
                # 回环/私网/链路本地段，CDN 域名重定向不受影响）
                if _is_internal_host(res.url.host):
                    raise RuntimeError("下载重定向指向内网地址，已中止")
                pending = bytearray()
                first_chunk = True
                async for chunk in res.content.iter_chunked(_DOWNLOAD_CHUNK_BYTES):
                    if first_chunk:
                        head = chunk[:32].decode("utf-8", errors="ignore").lower()
                        # HTML 之外还要拦 JSON/文本错误体：上游 200 + {"code":404,...}
                        # 这类响应超 256 字节，只认 HTML 会把它当音频落盘，
                        # 错误延迟到 ffmpeg/发送阶段才暴露、文案失真
                        if "<html" in head or "<!doctype" in head or head.lstrip().startswith(("{", "[")):
                            raise RuntimeError("下载内容不是音频（HTML/JSON 错误体），音频链接可能已失效")
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
                break
        else:
            raise RuntimeError("下载重定向次数超限，已中止")
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
