"""共享 HTTP 基础设施：复用 aiohttp 会话、宽松取值、安全 URL。

ncm / kg 两个 HTTP 客户端共用同一个 ``aiohttp.ClientSession``（模块级懒加载，
service.terminate() 统一关闭）；音频下载与封面取色也复用它。
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from typing import Any

import aiohttp
from astrbot.api import logger

from ..errors import ApiError

API_TIMEOUT_SEC = 20
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_session: aiohttp.ClientSession | None = None
_session_lock = threading.Lock()

# 连接池上限与兜底超时：漏传 timeout= 的调用点会永久挂起（aiohttp 默认总超时 5 分钟，
# 但 socket 层可无限），三源聚合 + 30 首连播下 100 连接默认值也容易被占满
_CONNECT_LIMIT = 64
_TIMEOUT_TOTAL = 30
_TIMEOUT_CONNECT = 10


def get_session() -> aiohttp.ClientSession:
    """共享会话。首次创建用线程锁保护：两个协程同时通过 _session is None 判断
    会各自建一个 ClientSession，后者覆盖前者 → 前者的连接池永远不会被 close。
    创建与 close_session 的置 None 共用同一把锁串行（见 close_session）。"""
    global _session
    if _session is not None and not _session.closed:
        return _session
    with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=_CONNECT_LIMIT, ttl_dns_cache=300),
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_TOTAL, connect=_TIMEOUT_CONNECT),
                headers={"User-Agent": USER_AGENT},
            )
    return _session


async def close_session() -> None:
    global _session
    # 先在锁内摘走引用并置 None，再在锁外 await close：置 None 与 get_session 的
    # 创建互斥后，close 期间迟到的 get_session 只会新建一个全新会话（terminate 之后
    # 的迟到调用属可接受行为），不会把正在关闭的旧会话当活会话复用。
    # 快路径（已有活会话直接返回）仍是无锁读，close 前一瞬拿走旧引用的调用方
    # 会随旧会话关闭收到一次请求失败，属可容忍窗口
    with _session_lock:
        sess = _session
        _session = None
    if sess is not None and not sess.closed:
        await sess.close()


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
            out[k] = str(v)
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
    """逐项归一化 → 丢弃 None 的统一循环。"""
    if not isinstance(items, list):
        return []
    out = []
    for i, item in enumerate(items[:limit] if limit else items):
        try:
            norm = normalizer(item, i)
        except Exception as e:  # noqa: BLE001 - 单条归一失败跳过整条，但留痕便于排查
            logger.debug(f"[music_hub] 归一化丢弃第 {i} 条: {type(e).__name__}: {e}")
            continue
        if norm:
            out.append(norm)
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
    """JSON 解析 + 畸形响应防御（非 JSON / 非 dict）。"""
    try:
        body = await res.json(content_type=None)
    except Exception:
        # 报文原文只落日志（apiBase 配错指向别的 web 服务时会带 HTML/内网信息），
        # 用户侧给固定文案，细节不进聊天
        text = (await res.text())[:200]
        logger.debug(f"[music_hub] {source} {safe_url(pathname)} 非 JSON 响应（HTTP {res.status}）：{text}")
        raise ApiError(f"API 服务返回异常数据（HTTP {res.status}）", source=source) from None
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
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)
    try:
        sess = get_session()
        if method == "get":
            async with sess.get(url, params=query_safe(params), timeout=timeout) as res:
                return res.status, await _json_body(res, source, pathname)
        async with sess.post(url, data=query_safe(params), timeout=timeout) as res:
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
