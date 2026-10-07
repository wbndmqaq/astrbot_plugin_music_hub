"""调用统计：按 天/音源/动作 计数 + 最近调用流水。

数据落 ``data/plugin_data/<plugin>/stats.json``，内存操作、延迟落盘
（脏标记 + 周期 flush）；统计失败只丢数据，不影响主流程。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from . import SOURCES

_TAG = "[music_hub]"

ACTIONS = (
    "search",
    # 分类型搜索（WebUI 标签页）：search.py 用 f"search_{type_}" 上报，
    # 未登记的动作名会被 record() 归到 "other"，导致这些分类在统计图里消失
    "search_playlist",
    "search_album",
    "search_artist",
    "play",
    "url",
    "lyric",
    "lyric_karaoke",
    "detail",
    "comment",
    "mv",
    "rank",
    "playlist",
    "explore",
    "login",
    "resolve",
    "other",
)

ACTION_NAMES = {
    "search": "搜索",
    "search_playlist": "歌单搜索",
    "search_album": "专辑搜索",
    "search_artist": "歌手搜索",
    "play": "点歌播放",
    "url": "取流",
    "lyric": "歌词",
    "lyric_karaoke": "逐字歌词",
    "detail": "歌曲详情",
    "comment": "评论",
    "mv": "MV",
    "rank": "排行榜",
    "playlist": "歌单",
    "explore": "浏览",
    "login": "登录",
    "resolve": "链接解析",
    "other": "其他",
}

MAX_RECENT = 120
FLUSH_INTERVAL = 8.0


def _day_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time()).strftime("%Y-%m-%d")


def _sum(node: dict, src: str) -> int:
    """某节点下某音源的调用总数。脏数据（字段被写成 list/None）时归零而非抛错。"""
    per = node.get(src) if isinstance(node, dict) else None
    return sum(per.values()) if isinstance(per, dict) else 0


def _actions(node: dict, src: str) -> dict:
    per = node.get(src) if isinstance(node, dict) else None
    return dict(per) if isinstance(per, dict) else {}


# 失败原因归类：上游原文里可能带 host:port 等内部地址，不适合直接回显给 WebUI。
# 顺序敏感：更具体的规则在前（"未登录" 要排在 "登录" 前）。
_ERROR_RULES = (
    ("未登录", "登录已失效"),
    ("凭证已过期", "登录已失效"),
    ("需要登录", "登录已失效"),
    ("扫码", "登录问题"),
    ("风控", "触发风控"),
    ("rate limit", "触发风控"),
    ("超时", "请求超时"),
    ("timeout", "请求超时"),
    ("timed out", "请求超时"),
    ("connection", "网络异常"),
    ("connect", "网络异常"),
    ("网络", "网络异常"),
    ("没有找到", "无匹配结果"),
    ("未配置", "音源未配置"),
)


def _error_bucket(detail: str | None) -> str:
    text = (detail or "").strip()
    if not text:
        return "未知原因"
    low = text.lower()
    for needle, label in _ERROR_RULES:
        if needle in low:
            return label
    return "其他错误"


class Stats:
    def __init__(self, data_dir: Path, retention_days: int = 30, enabled: bool = True):
        self._path = data_dir / "stats.json"
        self._retention = max(1, retention_days)
        self.enabled = enabled
        self._lock = asyncio.Lock()
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        self.daily: dict[str, dict] = {}  # day -> {source: {action: count}}
        self.totals: dict[str, dict] = {}  # source -> {action: count}
        self.recent: list[dict] = []  # 最近调用流水

    # ──────────── 持久化 ────────────
    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
            if isinstance(data, dict):
                self.daily = data.get("daily", {}) if isinstance(data.get("daily"), dict) else {}
                self.totals = data.get("totals", {}) if isinstance(data.get("totals"), dict) else {}
                recent = data.get("recent")
                self.recent = (
                    [r for r in recent if isinstance(r, dict)][-MAX_RECENT:]
                    if isinstance(recent, list)
                    else []
                )
        except (OSError, ValueError):
            pass
        self._prune()

    def _prune(self) -> None:
        cutoff = (datetime.now() - timedelta(days=self._retention)).strftime("%Y-%m-%d")
        self.daily = {k: v for k, v in self.daily.items() if k >= cutoff}
        for per in self.totals.values():
            if isinstance(per, dict):
                for action in list(per.keys()):
                    if action not in ACTIONS:
                        per.pop(action, None)

    async def start(self) -> None:
        await asyncio.to_thread(self._load)  # 恢复历史统计（文件读不占事件循环）
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL)
                await self.flush()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 - 循环体任何一环异常都只记日志，
                # 循环本身不能死：死掉后统计只进内存，重载全部丢失且无告警
                logger.warning(f"{_TAG} 统计刷写循环异常（继续）: {e}")

    async def flush(self) -> None:
        """落盘（脏标记防抖）。锁内先深拷贝快照再放线程池写——record() 会在
        事件循环上并发改同一批 dict，浅引用序列化时会撞 RuntimeError。"""
        async with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            payload = copy.deepcopy(
                {"daily": self.daily, "totals": self.totals, "recent": self.recent[-MAX_RECENT:]}
            )
        try:
            await asyncio.to_thread(self._write_payload, payload)
        except asyncio.CancelledError:
            # 取消也要恢复脏标记：CancelledError 是 BaseException，
            # 不恢复会让这批统计永久丢失（插件重载恰好落在刷写窗口内时）
            self._dirty = True
            raise
        except Exception as e:  # noqa: BLE001 - 落盘失败不致命，标脏下轮重试
            self._dirty = True
            logger.warning(f"{_TAG} 统计落盘失败: {e}")

    def _write_payload(self, payload: dict) -> None:
        # tmp 名带随机后缀（同 session.py 的做法）：固定名会被并发的孤儿写盘
        # 互踩——线程池里上一轮还没 replace，下一轮已经覆盖写同一个 tmp
        tmp = self._path.with_suffix(f".{os.urandom(4).hex()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
        tmp.replace(self._path)

    async def close(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()

    # ──────────── 记录 ────────────
    def record(self, source: str, action: str, ok: bool = True, detail: str = "") -> None:
        """记录一次调用。

        线程约束：仅可在事件循环线程调用（AstrBot 单事件循环模型下天然满足）。
        :meth:`flush` 会在锁内深拷贝快照后再交线程池写盘，故与本方法的并发是安全的。
        """
        if not self.enabled or source not in SOURCES:
            return
        if action not in ACTIONS:
            action = "other"
        day = _day_key()
        day_node = self.daily.setdefault(day, {})
        src_node = day_node.setdefault(source, {})
        src_node[action] = src_node.get(action, 0) + 1
        tot_node = self.totals.setdefault(source, {})
        tot_node[action] = tot_node.get(action, 0) + 1
        self.recent.append(
            {
                "ts": int(time.time()),
                "source": source,
                "action": action,
                "ok": bool(ok),
                "detail": (detail or "")[:80],
            }
        )
        if len(self.recent) > MAX_RECENT:
            self.recent = self.recent[-MAX_RECENT:]
        self._dirty = True

    def reset(self, keep_totals: bool = True) -> None:
        if keep_totals:
            self.daily = {}
            self.recent = []
        else:
            self.daily = {}
            self.totals = {}
            self.recent = []
        self._dirty = True

    # ---- 汇总输出（WebUI / 状态卡）----
    def day_total(self, source: str | None = None, day: str | None = None) -> int:
        day = day or _day_key()
        node = self.daily.get(day, {})
        if source:
            return sum((node.get(source, {}) or {}).values())
        return sum(sum((v or {}).values()) for v in node.values() if isinstance(v, dict))

    def total(self, source: str | None = None) -> int:
        if source:
            return sum((self.totals.get(source, {}) or {}).values())
        return sum(sum((v or {}).values()) for v in self.totals.values() if isinstance(v, dict))

    def snapshot(self, days: int = 14) -> dict:
        """WebUI 用：today/totals + 最近 N 天时间序列 + 流水。"""
        out_days = []
        for i in range(days - 1, -1, -1):
            key = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            node = self.daily.get(key, {})
            out_days.append({"date": key, "label": key[5:], **{src: _sum(node, src) for src in SOURCES}})
        today_node = self.daily.get(_day_key(), {})

        # 失败原因只归类，不回显上游原文：str(e) 里可能带 host:port 等内部地址
        errs: dict[str, int] = {}
        for r in reversed(self.recent):
            if r.get("ok"):
                continue
            key = _error_bucket(r.get("detail"))
            errs[key] = errs.get(key, 0) + 1
        top_errors = [{"detail": k, "count": v} for k, v in sorted(errs.items(), key=lambda kv: -kv[1])[:5]]

        return {
            "today": {src: _sum(today_node, src) for src in SOURCES},
            "todayTotal": self.day_total(),
            "total": {src: _sum(self.totals, src) for src in SOURCES},
            "totalAll": self.total(),
            "byActionToday": {src: _actions(today_node, src) for src in SOURCES},
            "days": out_days,
            "recent": list(reversed(self.recent[-60:])),
            "topErrors": top_errors,
            "retentionDays": self._retention,
        }
