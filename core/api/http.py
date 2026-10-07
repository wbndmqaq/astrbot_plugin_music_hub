"""共享 HTTP 基础设施：复用 aiohttp 会话、宽松取值、安全 URL。

ncm / kg 两个 HTTP 客户端共用同一个 ``aiohttp.ClientSession``（模块级懒加载，
service.terminate() 统一关闭）；音频下载与封面取色也复用它。
"""

from __future__ import annotations

import math
import threading
from typing import Any

import aiohttp
from astrbot.api import logger

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
    会各自建一个 ClientSession，后者覆盖前者 → 前者的连接池永远不会被 close。"""
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
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


def num(v: Any) -> float:
    """宽松转 float：None/容器/非数字/NaN/inf 一律 0（上游字段形态不稳定）。"""
    if v is None or isinstance(v, (list, dict, bool)):
        return 0
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
