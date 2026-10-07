"""定时任务：每日自动签到（网易云）+ 登录凭证保活刷新（QQ）。

- 每天固定时刻执行一次（默认 08:30，可配置 ``scheduler.signinHour`` 0-23 + 随机分钟）；
  任一子任务异常时按 :attr:`RETRY_COOLDOWN` 当日重试（重试由 _loop 驱动）
- 网易云签到：仅已登录时调用 /daily_signin，结果写日志
- QQ 凭证保活：refresh_credential 失败仅告警（下次扫码即可恢复）
- 循环任务在 service.terminate() 取消；任何异常不得中断循环
"""

from __future__ import annotations

import asyncio
import datetime
import random
import time

from astrbot.api import logger

TAG = "[music_hub]"

CHECK_INTERVAL = 300  # 每 5 分钟检查一次是否到点
# 当日已跑标记的 KV 键：纯内存标记重载即丢，当天会重复签到/日推
_KV_LAST_RUN = "sched_last_run_v1"


class Scheduler:
    # 单日任务失败后的重试间隔下限：_run_daily 失败时不置位 _last_run_date，
    # 若不额外限流就会每 5 分钟重跑一次，一天最多撞上游 288 次。
    RETRY_COOLDOWN = 3600.0

    def __init__(self, service):
        self._service = service
        self._task: asyncio.Task | None = None
        self._last_run_date: str = ""
        self._minute_date: str = ""
        self._minute: int = 0
        self._retry_after: float = 0.0

    async def start(self) -> None:
        if not self._service.config.scheduler_enable:
            return
        # 启动时恢复当日已跑标记：重载后不再把同一天当成没跑过
        self._last_run_date = await self._service.get_kv(_KV_LAST_RUN) or ""
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _target_time(self, now: datetime.datetime) -> datetime.datetime:
        hour = max(0, min(23, int(self._service.config.scheduler_signin_hour)))
        return now.replace(hour=hour, minute=self._daily_minute(now), second=0, microsecond=0)

    def _daily_minute(self, now: datetime.datetime) -> int:
        """每天抽一次随机分钟并缓存到当日；若每轮循环重抽，触发时刻会集中偏向
        整点前最后一个检查槽，「随机错峰」就名存实亡了。"""
        day = now.strftime("%Y-%m-%d")
        if self._minute_date != day:
            self._minute_date = day
            self._minute = random.randint(0, 59)
        return self._minute

    async def _loop(self) -> None:
        logger.info(f"{TAG} 定时任务已启动（每日签到/凭证保活）")
        while True:
            try:
                now = datetime.datetime.now()
                today = now.strftime("%Y-%m-%d")
                target = self._target_time(now)
                if now >= target and self._last_run_date != today:
                    if time.monotonic() < self._retry_after:
                        pass  # 上一轮失败后还在冷却期，本轮不重试
                    else:
                        try:
                            await self._run_daily()
                        except Exception as e:  # noqa: BLE001
                            # 不置位 _last_run_date，允许稍后重试；但要限流，
                            # 否则每 5 分钟一轮会一直撞上游风控。
                            self._retry_after = time.monotonic() + self.RETRY_COOLDOWN
                            logger.warning(f"{TAG} 每日任务失败，{self.RETRY_COOLDOWN // 60} 分钟后重试：{e}")
                        else:
                            self._last_run_date = today
                            # 当日全部任务成功才落盘：失败路径保持未置位，稍后重试
                            await self._service.put_kv(_KV_LAST_RUN, today)
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 循环本身不能死，明天还要签
                logger.warning(f"{TAG} 定时任务异常（继续运行）：{e}")
                await asyncio.sleep(CHECK_INTERVAL)

    async def _run_daily(self) -> None:
        cfg = self._service.config
        results: list[str] = []
        failed = False
        # 顺手清掉过期分页缓存：PAGER_TTL 只在读取时惰性生效，
        # 写入后未再访问的条目会一直留在内存里
        self._service.history.sweep_pagers()
        # 网易云每日签到
        if cfg.src_enabled("ncm") and cfg.src_cookie("ncm") and cfg.scheduler_ncm_signin:
            try:
                msg = await self._service.client_of("ncm").daily_signin()
                results.append(f"网易云签到：{msg}")
            except Exception as e:  # noqa: BLE001
                failed = True
                results.append(f"网易云签到失败：{e}")
        # QQ 凭证保活：返回 False（key 已失效需重新扫码）重试也无法恢复，
        # 只告警不上抛；只有异常（网络/服务故障）才值得当日重试
        if cfg.src_enabled("qq") and cfg.src_cookie("qq") and cfg.scheduler_qq_refresh:
            try:
                ok = await self._service.client_of("qq").refresh_credential()
                results.append(f"QQ 凭证保活：{'成功' if ok else '失败（需重新扫码）'}")
            except Exception as e:  # noqa: BLE001
                failed = True
                results.append(f"QQ 凭证保活异常：{e}")
        # 订阅推送（日推 / 歌手新歌）
        try:
            await self._service.subs.push_all()
            results.append("订阅推送：完成")
        except Exception as e:  # noqa: BLE001
            failed = True
            results.append(f"订阅推送失败：{e}")
        if results:
            logger.info(f"{TAG} 每日任务{'部分失败' if failed else '完成'} → " + "；".join(results))
        if failed:
            # 失败必须上抛而非静默吞掉：_loop 的 RETRY_COOLDOWN 重试机制只有收到
            # 异常才可达（原先全吞导致失败日也置位已跑标记，重试机器形同虚设）。
            # 摘要带上成功项，一条日志即可看到整轮各任务的下场。
            # 与 ncm 侧的配合：同日重试会再次调用 daily_signin，已签到的账号会拿到
            # 嵌套 code=-2 的「重复签到」响应——该响应由 ncm 客户端识别为已完成
            # 而非失败（客户端侧另行修复），本模块只保证失败日的重试真的发生。
            raise RuntimeError("；".join(results))
