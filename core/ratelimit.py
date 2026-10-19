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
        # 惰性间隔来源（config.rate_limit_ms 的 getter）：主面板改配置不通知插件、
        # WebUI 保存钩子只覆盖面板自身路径——每次 acquire 现读才全覆盖
        self._provider = None
        self._next_ok: dict[str, float] = {s: 0.0 for s in SOURCES}
        self._lock = asyncio.Lock()

    def set_interval_provider(self, fn) -> None:
        self._provider = fn

    def update_interval(self, interval_ms: float) -> None:
        self._interval = max(0.0, float(interval_ms)) / 1000.0

    async def acquire(self, source: str) -> None:
        if self._provider is not None:
            # getter 是 config 属性读取（as_int 包裹），无失败面
            self._interval = max(0.0, float(self._provider())) / 1000.0
        if self._interval <= 0 or source not in self._next_ok:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_ok[source] - now
            # wait>0 时预约对窗口的净增量就是 interval（在旧值上 +interval）；
            # 取消只回拨这个增量才能还原到预约前的时刻。若减 wait+interval，
            # 窗口会被拨到 now——已有其他等待者接力预约时，会把窗口拨得比
            # 预约前更早，等效绕过一段限速
            step = self._interval
            if wait > 0:
                self._next_ok[source] = now + wait + step
            else:
                self._next_ok[source] = now + step
        if wait > 0:
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                # 睡眠期间被取消（聚合搜索连锁取消/terminate）：槽位已预约但请求
                # 没发出，把预约回拨——否则点歌台连播 + 用户切歌这类高频取消场景
                # 会虚耗配额，等效于人为抬高限速
                async with self._lock:
                    self._next_ok[source] = max(0.0, self._next_ok[source] - step)
                raise


# 共享实例（ncm/kg 客户端与 service 共用；间隔由 service 按配置更新）
limiter = RateLimiter(250.0)
