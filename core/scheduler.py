"""定时任务：每日自动签到（网易云）+ 登录凭证保活刷新（QQ）。

- 每天固定时刻执行一次（默认 08:30，可配置 ``scheduler.signinHour`` 0-23 + 随机分钟）
- 网易云签到：仅已登录时调用 /daily_signin，结果写日志
- QQ 凭证保活：refresh_credential 失败仅告警（下次扫码即可恢复）
- 循环任务在 service.terminate() 取消；任何异常不得中断循环
"""

from __future__ import annotations

import asyncio
import datetime
import random

from astrbot.api import logger

TAG = "[music_hub]"

CHECK_INTERVAL = 300  # 每 5 分钟检查一次是否到点


class Scheduler:
    def __init__(self, service):
        self._service = service
        self._task: asyncio.Task | None = None
        self._last_run_date: str = ""
        self._minute_date: str = ""
        self._minute: int = 0

    async def start(self) -> None:
        if not self._service.config.scheduler_enable:
            return
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
                    self._last_run_date = today
                    await self._run_daily()
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单日任务失败只记日志，循环保住（明天还要签）
                logger.warning(f"{TAG} 定时任务异常（继续运行）：{e}")
                await asyncio.sleep(CHECK_INTERVAL)

    async def _run_daily(self) -> None:
        cfg = self._service.config
        results = []
        # 网易云每日签到
        if cfg.src_enabled("ncm") and cfg.src_cookie("ncm") and cfg.scheduler_ncm_signin:
            try:
                msg = await self._service.ncm.daily_signin()
                results.append(f"网易云签到：{msg}")
            except Exception as e:  # noqa: BLE001
                results.append(f"网易云签到失败：{e}")
        # QQ 凭证保活
        if cfg.src_enabled("qq") and cfg.src_cookie("qq") and cfg.scheduler_qq_refresh:
            try:
                ok = await self._service.qq.refresh_credential()
                results.append(f"QQ 凭证保活：{'成功' if ok else '失败（需重新扫码）'}")
            except Exception as e:  # noqa: BLE001
                results.append(f"QQ 凭证保活异常：{e}")
        # 订阅推送（日推 / 歌手新歌）
        try:
            await self._service.subs.push_all()
            results.append("订阅推送：完成")
        except Exception as e:  # noqa: BLE001
            results.append(f"订阅推送失败：{e}")
        if results:
            logger.info(f"{TAG} 每日任务完成 → " + "；".join(results))
