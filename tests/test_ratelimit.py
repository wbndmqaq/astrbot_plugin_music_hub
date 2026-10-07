"""core.ratelimit 的直接覆盖（P2-13）：带锁的跨源节流防线，并发语义最容易在重构中坏掉。

直接实例化 RateLimiter 而不用模块级单例 ``limiter``：单例的调度状态
（_next_ok）跨测试残留，隔离只能靠重建实例。
"""

import asyncio
import time

from astrbot_plugin_music_hub.core.ratelimit import RateLimiter


async def test_concurrent_acquires_are_spaced_by_interval():
    """多协程同时 acquire：首个立即放行，之后按请求序每两个之间间隔 ≥ interval。"""
    rl = RateLimiter(120.0)
    done: list[float] = []

    async def grab():
        await rl.acquire("ncm")
        done.append(time.monotonic())

    await asyncio.gather(*(grab() for _ in range(4)))
    done.sort()
    assert len(done) == 4
    # 间隔断言的目的是挡「完全不节流 / 只挡第一次」两类退化（间隔≈0），不是校准节流精度：
    # done 记的是 acquire 返回后的时刻，高负载下会滞后于真实放行，把测得间隔压小，
    # 所以下限取 interval 的 ~40% 而不是贴着 0.12 —— 贴紧必在 CI/多任务机器上抖
    for prev, cur in zip(done, done[1:], strict=False):
        assert cur - prev >= 0.05, f"相邻放行间隔 {cur - prev:.3f}s，节流疑似失效"
    # 同理取 3×interval 的一半多：退化实现（只挡第一次）的跨度趋近 0，不会误伤
    assert done[-1] - done[0] >= 0.20


async def test_update_interval_takes_effect():
    """update_interval 覆盖构造间隔：第二起等待按新间隔计。"""
    rl = RateLimiter(1000.0)
    rl.update_interval(60.0)
    t0 = time.monotonic()
    await rl.acquire("kg")
    await rl.acquire("kg")
    elapsed = time.monotonic() - t0
    # 第二次放行等的是新间隔 60ms；update 未生效（仍 1000ms）会远超 0.3s，
    # 下限 0.05 防退化成完全不限速
    assert 0.05 <= elapsed < 0.3, f"间隔 {elapsed:.3f}s 不符合更新后的 60ms"


async def test_update_interval_zero_disables_limiting():
    rl = RateLimiter(200.0)
    rl.update_interval(0)
    t0 = time.monotonic()
    await asyncio.gather(*(rl.acquire("ncm") for _ in range(5)))
    assert time.monotonic() - t0 < 0.1, "间隔置 0 后应完全放行"


async def test_unknown_source_passes_through():
    """未登记的 source 直接放行（限速表按内置三源建，未知源不该被卡死）。"""
    rl = RateLimiter(3000.0)
    t0 = time.monotonic()
    await rl.acquire("spotify")
    assert time.monotonic() - t0 < 0.1


async def test_sources_are_throttled_independently():
    """不同源各计各的节流：一个源刚放行不拖累另一个源立即放行。"""
    rl = RateLimiter(200.0)
    t0 = time.monotonic()
    await rl.acquire("ncm")
    await rl.acquire("kg")
    await rl.acquire("qq")
    assert time.monotonic() - t0 < 0.1, "不同源的 acquire 不应互相等待"
