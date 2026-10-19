"""共享 HTTP 基础设施：复用 aiohttp 会话、宽松取值、安全 URL。

ncm / kg 两个 HTTP 客户端共用同一个 ``aiohttp.ClientSession``（模块级懒加载，
service.terminate() 统一关闭）；音频下载与封面取色也复用它。
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import aiohttp
from astrbot.api import logger

from ..errors import ApiError

# 请求级 JSON 请求超时（搜索/取流等业务请求）；与 session 级 _TIMEOUT_TOTAL 的分工：
# 后者是漏传 timeout 的调用点（音频下载、封面取色）的兜底，前者是显式收窄的业务超时。
# 缺省 20s；service 启动时注入 config 读取器（api_timeout_sec），改配置即时生效
_DEFAULT_API_TIMEOUT_SEC = 20


def _default_timeout() -> int:
    return _DEFAULT_API_TIMEOUT_SEC


_timeout_provider: Callable[[], int] = _default_timeout


def set_api_timeout_provider(getter: Callable[[], int]) -> None:
    """注入超时秒数来源（无参 → int）。模块级函数而非参数透传：调用方遍布三源
    客户端，逐层传参会污染全部 request 签名。"""
    global _timeout_provider
    _timeout_provider = getter


def _api_timeout_sec() -> int:
    try:
        return max(1, int(_timeout_provider()))
    except Exception:  # noqa: BLE001 - 配置异常按缺省跑，超时取错只影响快慢
        return _DEFAULT_API_TIMEOUT_SEC


# 上游 JSON 响应体上限：aiohttp 会自动解压 gzip 且 res.json() 先全量进内存，
# apiBase 配错指向大文件/大页面时是无上限的内存放大点；正常音源 JSON 远小于此
_MAX_JSON_BYTES = 8 * 1024 * 1024
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_session: aiohttp.ClientSession | None = None
_session_lock = threading.Lock()
# terminate 之后的「已关闭」哨兵：close_session 与 get_session 的竞态窗口里，
# 迟到请求会新建一个永远无人 close 的会话；置位后直接拒绝新会话创建
_closed = False

# 连接池上限与兜底超时：漏传 timeout= 的调用点会永久挂起（aiohttp 默认总超时 5 分钟，
# 但 socket 层可无限），三源聚合 + 30 首连播下 100 连接默认值也容易被占满
_CONNECT_LIMIT = 64
_TIMEOUT_TOTAL = 30
_TIMEOUT_CONNECT = 10


def get_session() -> aiohttp.ClientSession:
    """共享会话。首次创建用线程锁保护：两个协程同时通过 _session is None 判断
    会各自建一个 ClientSession，后者覆盖前者 → 前者的连接池永远不会被 close。
    创建与 close_session 的置 None 共用同一把锁串行（见 close_session）。

    注意：锁内禁止加入任何 IO/await——threading.Lock 在事件循环内阻塞会卡住整个 loop。"""
    global _session
    if _session is not None and not _session.closed:
        return _session
    with _session_lock:
        if _closed:
            raise ApiError("插件正在关闭，HTTP 会话已停止", source="")
        if _session is None or _session.closed:
            # CookieJar(unsafe=True)：默认 jar 对 IP 主机一律拒收 Set-Cookie，而
            # 本地/内网部署的 apiBase 恰恰是 IP 形态（http://127.0.0.1:3000 最常见）
            # ——酷狗 /register/dev 的设备 cookie（dfid/KUGOU_API_*）会被静默丢弃，
            # 设备指纹永不落盘、每次重载都注册新设备（风控放大）。会话只与自建的
            # 上游 API 服务通信，不存在「第三方 IP 域名种 cookie」的跨站向量
            _session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=_CONNECT_LIMIT, ttl_dns_cache=300),
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_TOTAL, connect=_TIMEOUT_CONNECT),
                headers={"User-Agent": USER_AGENT},
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
    return _session


async def close_session() -> None:
    global _session, _closed
    # 先在锁内摘走引用并置 None，再在锁外 await close：置 None 与 get_session 的
    # 创建互斥后，close 期间迟到的 get_session 不再新建孤儿会话（_closed 拦下），
    # 也不会把正在关闭的旧会话当活会话复用。
    # 快路径（已有活会话直接返回）仍是无锁读，close 前一瞬拿走旧引用的调用方
    # 会随旧会话关闭收到一次请求失败，属可容忍窗口
    with _session_lock:
        sess = _session
        _session = None
        _closed = True
    if sess is not None and not sess.closed:
        await sess.close()


def reset_http() -> None:
    """解除 close_session 的「已关闭」哨兵。

    AstrBot 的「停用→启用」在同进程内只重跑 initialize()、不重载本模块：不复位的
    话 _closed 一次性闩会让之后所有音源请求永远报「插件正在关闭」。必须与
    close_session 互斥（避免复位瞬间把正在进行的关闭撤销成半开会话）。"""
    global _closed
    with _session_lock:
        _closed = False


def num(v: Any) -> float:
    """宽松转 float：None/容器/非数字/NaN/inf 一律 0（上游字段形态不稳定）。
    bool 按 int(b) 计（True→1），与 opt_int 保持同一语义，不做两种转换。"""
    if v is None or isinstance(v, (list, dict)):
        return 0
    # bool 是 int 子类，float(True)=1.0
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0
    return f if math.isfinite(f) else 0


def opt_int(v: Any) -> int | None:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        return None


def safe_url(url: str) -> str:
    """日志/报错用：去掉 query（ncm 以 query 传 cookie，泄进日志等于泄号）。"""
    return str(url or "").split("?", 1)[0]


def query_safe(params: dict) -> dict:
    out: dict = {}
    for k, v in params.items():
        if v is None:
            continue
        if isinstance(v, bool):
            out[k] = int(v)
        elif isinstance(v, (str, int, float)):
            out[k] = v
        else:
            # list/dict 等容器值是参数组装错误：str() 会把 Python repr 发给服务端，
            # 跳过不传比发垃圾参数安全
            continue
    return out


def merge_cookie(base: str, extra: str) -> str:
    """合并 cookie 串（k=v; k=v），extra 覆盖 base 同名键。"""
    if not base:
        return extra
    if not extra:
        return base
    merged: dict[str, str] = {}
    for raw in (base, extra):
        for part in str(raw).split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                merged[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def data_of(body: Any) -> dict:
    """body.data 子字典；非 dict（列表/字符串）一律 {}（上游畸形返回防御）。"""
    if not isinstance(body, dict):
        return {}
    d = body.get("data")
    return d if isinstance(d, dict) else {}


def list_of(value: Any) -> list:
    return value if isinstance(value, list) else []


def collect(items: Any, normalizer, limit: int | None = None) -> list:
    """逐项归一化 → 丢弃 None 的统一循环。

    先归一化后截断：畸形条目可能集中在头部（上游广告位/占位对象），先截断会让
    它们吃掉 limit 名额、正常数据反被丢弃，返回数少于 limit 且不可解释。"""
    if not isinstance(items, list):
        return []
    out = []
    dropped = 0
    # limit 给定时也不能只扫前 limit 条：畸形头部会把配额吃光。扫全量、凑满
    # limit 即停——畸形比例极端时最多多扫一遍全表，成本可忽略
    for i, item in enumerate(items):
        if limit is not None and len(out) >= limit:
            break
        try:
            norm = normalizer(item, i)
        except Exception as e:  # noqa: BLE001 - 单条归一失败跳过整条，但留痕便于排查
            dropped += 1
            logger.debug(f"[music_hub] 归一化丢弃第 {i} 条: {type(e).__name__}: {e}")
            continue
        if norm:
            out.append(norm)
    # 全量丢弃对用户表现为「搜不到」，仅 debug 级排障时几乎找不到线索，升到 warning
    if dropped and not out:
        logger.warning(
            f"[music_hub] 归一化丢弃 {dropped}/{len(items)} 条，本源结果为空（上游格式可能已变化）"
        )
    # 丢弃畸形条目会让 index 出现空洞（display 序号从 4 起之类）：统一重编为连续序号。
    # 所有调用方（song/playlist/album/artist/comment 归一器）的 index 都是纯展示序号
    for i, row in enumerate(out):
        if isinstance(row, dict) and "index" in row:
            row["index"] = i + 1
    return out


# ──────────── HTTP 音源共用传输骨架（ncm / kg；qq 走内置库不经此） ────────────


def with_ts(params: dict | None = None) -> dict:
    """api-enhanced 对 200 响应缓存 2 分钟：登录/写操作类请求拼 timestamp 穿透。"""
    out = dict(params or {})
    out["timestamp"] = int(time.time() * 1000)
    return out


def format_duration(value: float, unit: str = "auto") -> str:
    """时长 → mm:ss。unit="ms" 恒按毫秒；"auto" 是兼容未知单位的阈值猜测：≥10000
    视为毫秒、否则视为秒。猜测的边界（已知局限）：小于 10s 的音频（如 9000ms 试听）
    会被当成 9000 秒格式化成 150:00；超过 10000s（约 2h47m）的秒值会被当成毫秒。
    单位依据：timelength 系字段恒为毫秒（上游 audio_match 页 formatDuration 按 ms
    处理、插件 dtMs 亦按 ms 消费），可显式传 "ms"；搜索结果的 Duration/duration
    系字段单位在上游代理源码中无从核实，只能保留 "auto"。"""
    if value <= 0:
        return ""
    if unit == "ms" or (unit == "auto" and value >= 10000):
        value /= 1000
    sec = int(value)
    return f"{sec // 60:02d}:{sec % 60:02d}"


async def _json_body(res: aiohttp.ClientResponse, source: str, pathname: str) -> dict:
    """JSON 解析 + 畸形响应防御（超限 / 非 JSON / 非 dict）。

    先限量读入内存再解析：res.json() 会把整个响应（含自动解压后的体积）读进内存，
    apiBase 配错指向大文件/大页面时是内存放大点，必须在读取阶段就封顶。"""
    # read(n) 只在缓冲区空时等待，缓冲区一有数据就原样返回（不凑满 n）——
    # 大响应被静默截断、json.loads 必失败的坑。必须循环 readany 读到 EOF，
    # 限量防护改为累计字节数判断
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await res.content.readany()
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_JSON_BYTES:
                raise ApiError(
                    f"API 服务响应超过 {_MAX_JSON_BYTES // (1024 * 1024)}MB 上限（HTTP {res.status}）",
                    source=source,
                )
            chunks.append(chunk)
    except aiohttp.ClientError as e:
        raise ApiError(f"API 服务响应读取失败（HTTP {res.status}）", source=source) from e
    raw = b"".join(chunks)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        # 报文原文只落日志（apiBase 配错指向别的 web 服务时会带 HTML/内网信息），
        # 用户侧给固定文案，细节不进聊天；异常类型进日志，排障不至于两眼一抹黑。
        # raw 已限量在 _MAX_JSON_BYTES 内，此处再截 512 字节只为日志可读性
        text = raw[:512].decode("utf-8", errors="replace")[:200]
        logger.debug(
            f"[music_hub] {source} {safe_url(pathname)} 非 JSON 响应（HTTP {res.status}，"
            f"{type(e).__name__}）：{text}"
        )
        # cause 换成脱敏摘要：原异常的 str 可能带完整 URL（含 query 里的 cookie），
        # 一旦未来有调用点用 exc_info=True 记录，会经 LogBuffer 泄进 WebUI 日志页
        raise ApiError(f"API 服务返回异常数据（HTTP {res.status}）", source=source) from ApiError(
            f"{type(e).__name__}（原文已脱敏）", source=source
        )
    if not isinstance(body, dict):
        raise ApiError(f"返回格式异常（HTTP {res.status}）", source=source)
    return body


async def request_json(
    source: str,
    label: str,
    base: str,
    pathname: str,
    params: dict,
    *,
    method: str = "get",
    net_err_with_url: bool = False,
) -> tuple[int, dict]:
    """限流 → 请求 → JSON 解析的共用骨架。返回 (status, body)，业务级错误码判定
    留给调用方的 _handle（ncm 顶层 code/301、kg error_code+status==0 差异太大，
    硬参数化只会逼出回调地狱）。

    net_err_with_url：ncm 的网络错误文案带 safe_url(url) 便于排障，kg 不带（历史上如此，
    保持文案不变）。
    """
    from ..ratelimit import limiter

    await limiter.acquire(source)
    url = f"{base}{pathname if pathname.startswith('/') else '/' + pathname}"
    # 请求级 timeout 整体替换 session 级超时（不是合并），connect 必须显式带上，
    # 否则 session 上配置的 _TIMEOUT_CONNECT 从不生效
    timeout = aiohttp.ClientTimeout(total=_api_timeout_sec(), connect=_TIMEOUT_CONNECT)
    try:
        sess = get_session()
        if method == "get":
            async with sess.get(url, params=query_safe(params), timeout=timeout) as res:
                return res.status, await _json_body(res, source, pathname)
        # POST 也走 query 而非表单体：这些 API 的签名/鉴权参数（可能含 cookie）按
        # query 语义处理，safe_url 剥 query 里的 cookie 也只对 URL 生效——写进 body
        # 会静默改变语义且错误文案/日志的脱敏路径全覆盖不到
        async with sess.post(url, params=query_safe(params), timeout=timeout) as res:
            return res.status, await _json_body(res, source, pathname)
    except aiohttp.ClientConnectorError as e:
        raise ApiError(f"无法连接{label}（{safe_url(base)}），请确认服务已启动", source=source) from e
    except aiohttp.ServerTimeoutError as e:
        raise ApiError("请求超时", source=source, timeout=True) from e
    except aiohttp.ClientError as e:
        detail = f"：{safe_url(url)}" if net_err_with_url else ""
        raise ApiError(f"网络错误（{type(e).__name__}）{detail}", source=source) from e
    except asyncio.TimeoutError as e:
        # ClientTimeout(total=...) 到期：3.10 抛 asyncio.TimeoutError，3.11 起与内建 TimeoutError 同一类型
        raise ApiError("请求超时", source=source, timeout=True) from e
    except RuntimeError as e:
        # terminate/reload 竞态窗口：复用已关闭的会话抛 RuntimeError("Session is closed")，
        # 插件重载换事件循环则是 "Event loop is closed"。二者都不在 aiohttp.ClientError
        # 体系内，不接住会裸穿到 handler 的兜底文案路径，重载时必现一小批
        if "closed" in str(e).lower():
            raise ApiError("插件正在关闭或重载中，请稍后重试", source=source) from e
        raise


def harden_sensitive_file(path) -> bool:
    """尽力收紧敏感凭据文件的访问面（对齐 webui_secret.key 的做法）。

    设备指纹/设备 cookie 是风控敏感数据（泄漏后可被关联画像、触发风控放大），
    QQ 与酷狗共用此 best-effort 收紧：失败不抛——磁盘/ACL 能力差异大，收紧失败
    只降低防护水平，不值得打断登录链路。
    返回是否收紧成功：调用方可据此决定是否保留文件（明文口令类内容应收紧失败
    即删，对齐 _auth._restrict_acl 的「弱保护不如无文件」取舍）。"""
    import os
    import subprocess
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return False
    try:
        p.chmod(0o600)
    except OSError:
        # docstring 契约「返回是否收紧成功」：chmod 失败必须如实上报，
        # 不能吞掉异常后落到下面的无条件 return True 虚报成功
        return False
    if os.name != "nt":
        return True
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        return False
    try:
        r = subprocess.run(
            ["icacls", str(p), "/inheritance:r", "/grant:r", f"{user}:F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
