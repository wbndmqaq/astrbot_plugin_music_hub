"""定时任务随机分钟（core.scheduler）—— 按日缓存错峰（v1.0.2 回归）。"""

import datetime

from astrbot_plugin_music_hub.core.scheduler import Scheduler


def make_scheduler() -> Scheduler:
    # 跳过 __init__（它依赖完整 service），只测分钟缓存逻辑
    s = Scheduler.__new__(Scheduler)
    s._minute_date = ""
    s._minute = 0
    return s


def test_minute_is_stable_within_a_day():
    s = make_scheduler()
    now = datetime.datetime(2026, 10, 1, 8, 0)
    first = s._daily_minute(now)
    assert s._daily_minute(now.replace(minute=55)) == first  # 同日不重抽


def test_minute_rerolls_next_day():
    s = make_scheduler()
    now = datetime.datetime(2026, 10, 1, 8, 0)
    s._daily_minute(now)
    nxt = s._daily_minute(now.replace(day=2, hour=9))
    assert isinstance(nxt, int) and 0 <= nxt <= 59
    assert s._minute_date == "2026-10-02"


def test_target_time_clamps_hour():
    s = make_scheduler()
    s._service = type("S", (), {"config": type("C", (), {"scheduler_signin_hour": 30})()})()
    target = s._target_time(datetime.datetime(2026, 10, 1, 5, 0))
    assert target.hour == 23  # 越界小时钳到 23
