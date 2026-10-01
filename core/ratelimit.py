"""音源请求限速器：每源最小间隔的异步节流（防突发触发风控）。

聚合搜索会并发打三个源；上游对突发敏感（ncm 460/503、kg 20028、qq Ratelimited）。
在客户端 request 前过一道 ``await limiter.acquire(source)``：令牌桶按源独立计数，
默认最小间隔 250ms，可被配置 ``rateLimitMs`` 覆盖（0 = 关闭）。
"""

from __future__ import annotations

import asyncio
import time

from . import SOURCES


class RateLimiter:
    def __init__(self, interval_ms: float = 250.0):
        self._interval = max(0.0, interval_ms) / 1000.0
        self._next_ok: dict[str, float] = {s: 0.0 for s in SOURCES}
        self._lock = asyncio.Lock()

    def update_interval(self, interval_ms: float) -> None:
        self._interval = max(0.0, float(interval_ms)) / 1000.0

    async def acquire(self, source: str) -> None:
        if self._interval <= 0 or source not in self._next_ok:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_ok[source] - now
            if wait > 0:
                self._next_ok[source] = now + wait + self._interval
            else:
                self._next_ok[source] = now + self._interval
        if wait > 0:
            await asyncio.sleep(wait)


# 共享实例（ncm/kg 客户端与 service 共用；间隔由 service 按配置更新）
limiter = RateLimiter(250.0)
