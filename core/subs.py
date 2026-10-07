"""订阅推送：每天定时把 日推（网易云）/ 订阅歌手新歌（酷狗）推送到群。

订阅表持久化在插件 KV（key=subs_v1）：{umo: {"scope", "daily", "artists": [...]}}。
推送走 context.send_message 文本（跨平台最稳）；歌手新歌去重靠 seen hash 环。
"""

from __future__ import annotations

import asyncio
import copy
import datetime

from astrbot.api import logger

from . import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM

TAG = "[music_hub]"
_KV_KEY = "subs_v1"
_SEEN_CAP = 200
_PUSH_LIMIT = 10


def _same_artist(want: str, got: str) -> bool:
    """歌手名精确匹配（与 remove_artist 对称）。

    只按归一化用的多歌手分隔符「 / 」拆分：「周杰伦&费玉清」是另一个独立歌手实体
    （可单独订阅/退订），不能因为包含「周杰伦」就算命中，否则退订后仍会继续推送。
    """
    if not want:
        return False
    return any(part.strip() == want for part in got.split("/"))


def _normalize_rows(data) -> dict:
    """逐行规范化 KV 数据：旧 schema / 手改 JSON 可能缺键或类型不对，
    直接用会在 set_daily / add_artist 里抛 KeyError 冒泡到指令 handler。"""
    if not isinstance(data, dict):
        return {}
    rows = {}
    for umo, row in data.items():
        if not isinstance(row, dict):
            continue
        artists = []
        for a in row.get("artists") or []:
            if isinstance(a, dict) and a.get("id"):
                artists.append(
                    {
                        "id": str(a["id"]),
                        "name": str(a.get("name", "")),
                        "seen": [str(x) for x in a.get("seen") or [] if x][-_SEEN_CAP:],
                    }
                )
        rows[str(umo)] = {
            "scope": str(row.get("scope", "")),
            "daily": bool(row.get("daily")),
            # 日推当日已推标记（YYYY-MM-DD）：定时任务失败会当日重试整轮，
            # 没有它每次重试都会把同一份日推重发给订阅者（最多十几次）
            "daily_date": str(row.get("daily_date") or ""),
            "artists": artists,
        }
    return rows


class Subscriptions:
    # 写锁：_subs 会被订阅/退订（聊天端）与 seen 累积（推送任务）同时改，
    # 不串行化的话序列化到一半的 dict 会被写坏。类级锁——测试用 __new__ 绕过
    # __init__ 造实例时也能用。
    _lock = asyncio.Lock()

    def __init__(self, service):
        self._service = service
        self._subs: dict[str, dict] = {}

    async def load(self) -> None:
        data = await self._service.get_kv(_KV_KEY)
        self._subs = _normalize_rows(data)

    async def _save(self) -> None:
        # 快照在调用方持锁时拷出：put_kv（shared_preferences put_async）入队时才
        # 对传入值做拷贝并异步落盘，若在锁外拷贝，快照可能夹带并发写入的中间态
        await self._service.put_kv(_KV_KEY, copy.deepcopy(self._subs))

    def _row(self, umo: str, scope: str) -> dict:
        return self._subs.setdefault(umo, {"scope": scope, "daily": False, "artists": []})

    # ──────────── 订阅管理 ────────────
    async def set_daily(self, umo: str, scope: str, on: bool) -> None:
        async with self._lock:
            row = self._row(umo, scope)
            row["daily"] = on
            if not on and not row["artists"]:
                self._subs.pop(umo, None)
            await self._save()

    async def add_artist(self, umo: str, scope: str, artist: dict) -> bool:
        """artist: {id, name}。重复订阅返回 False。"""
        async with self._lock:
            row = self._row(umo, scope)
            if any(a.get("id") == artist["id"] for a in row["artists"]):
                return False
            row["artists"].append({"id": str(artist["id"]), "name": artist.get("name", ""), "seen": []})
            await self._save()
            return True

    async def remove_artist(self, umo: str, scope: str, name: str) -> dict | None:
        """按名退订歌手。只做精确匹配——子串匹配会让退订「周杰伦」误删「周杰伦&费玉清」。"""
        async with self._lock:
            row = self._subs.get(umo)
            if not row:
                return None
            for i, a in enumerate(row["artists"]):
                if a.get("name") == name:
                    removed = row["artists"].pop(i)
                    if not row["artists"] and not row.get("daily"):
                        self._subs.pop(umo, None)
                    await self._save()
                    return removed
            return None

    def list_of(self, umo: str) -> dict | None:
        return self._subs.get(umo)

    # ──────────── 每日推送 ────────────
    async def push_all(self) -> None:
        if not self._service.config.enable:
            return  # 总开关关闭时连订阅推送一起静默
        today = datetime.date.today().isoformat()
        # 「日推文案」与「关注的歌手新歌」对所有订阅会话是同一份数据：整轮各取一次。
        # 每个会话各取一次是 N 倍请求，会撞酷狗 20028 风控。
        daily = None
        if any(row.get("daily") and row.get("daily_date") != today for row in list(self._subs.values())):
            daily = await self._push_daily()
        songs = None
        if any(row.get("artists") for row in list(self._subs.values())):
            songs = await self._fetch_followed_songs()
        for umo, row in list(self._subs.items()):
            try:
                await self._push_one(umo, row, songs, daily, today)
            except Exception as e:  # noqa: BLE001 - 单会话失败不影响其他
                logger.warning(f"{TAG} 订阅推送失败（{umo}）：{e}")

    async def _push_one(
        self, umo: str, row: dict, songs: list[dict] | None = None, daily: str | None = None, today: str = ""
    ) -> None:
        """推送单个会话。songs/daily 是 push_all 整轮各取一次的共享结果，生产路径
        恒传实参（push_all 对「任一行订阅了对应内容」预检后才取，订阅行必拿到实取值）；
        留默认 None 仅为测试直调留门——未预取时本调用内自行补取一次。

        日推按 daily_date 当日去重：定时任务失败（签到 403 等）会当日重试整轮，
        成功发送过一次（含失败占位文案）就不再重发，否则订阅者一天收到十几次重复日推。
        """
        if not today:
            today = datetime.date.today().isoformat()
        parts = []
        daily_due = bool(row.get("daily")) and row.get("daily_date") != today
        if daily_due:
            if daily is None:
                daily = await self._push_daily()
            parts.append(daily)
        artists = list(row.get("artists", []))
        fresh_sids: list[tuple[dict, list[str]]] = []
        if artists:
            if songs is None:
                songs = await self._fetch_followed_songs()
            for artist in artists:
                part, sids = await self._push_artist(artist, songs)
                if part:
                    parts.append(part)
                    fresh_sids.append((artist, sids))
        if not parts:
            return
        text = "♪ Music Hub 订阅推送\n" + "\n".join(parts) + "\n（退订：退订 日推 / 退订新歌 歌手名）"
        sent = await self._service.send_to_umo(umo, text)
        if sent is False:
            return  # 发送失败这批 sid 不记 seen：下轮重推，否则这批歌永远丢失
        if fresh_sids or daily_due:
            # 发送成功才把本批 sid / 当日标记并入并落盘；订阅/退订指令的写也在锁内，
            # 不串行化会丢更新。本段无 await 网络（_save 只入队），锁内循环不阻塞事件循环。
            async with self._lock:
                for artist, sids in fresh_sids:
                    merged = list(dict.fromkeys([*(artist.get("seen") or []), *sids]))
                    artist["seen"] = merged[-_SEEN_CAP:]
                if daily_due:
                    row["daily_date"] = today
                await self._save()

    async def _push_daily(self) -> str:
        try:
            songs = await self._service.client_of(SOURCE_NCM).daily_recommend()
        except Exception as e:  # noqa: BLE001
            return f"· 日推：获取失败（{e}）"
        if not songs:
            return "· 日推：今日暂无推荐（需网易云保持登录）"
        lines = [f"· 日推 TOP{min(len(songs), _PUSH_LIMIT)}"]
        for i, s in enumerate(songs[:_PUSH_LIMIT], 1):
            lines.append(f"  {i}. {s.get('name')} - {s.get('artist')}")
        return "\n".join(lines)

    async def _push_artist(self, artist: dict, songs: list[dict] | None = None) -> tuple[str, list[str]]:
        """单个歌手的新歌文案与本批 sid。

        songs 是「我关注的全部歌手的新歌」的共享结果：每个歌手各请求一次纯属浪费
        （N 倍请求撞酷狗 20028 风控），调用方应取一次后传给所有歌手；传 None 时
        本调用经 _fetch_followed_songs 补取一次（仅测试直调路径，生产唯一调用方
        _push_one 恒传实参）。

        只计算不落 seen：sid 由 _push_one 在发送成功后合并入库。"""
        if songs is None:
            songs = await self._fetch_followed_songs()
        name = artist.get("name", "")
        seen = set(artist.get("seen") or [])
        # 歌手名精确匹配（与 remove_artist 一致）：子串匹配会让订阅「周杰伦」
        # 连带推送「周杰伦&费玉清」的歌
        fresh = [
            s
            for s in songs
            if s.get("sid") and s["sid"] not in seen and _same_artist(name, s.get("artist") or "")
        ]
        if not fresh:
            return "", []
        shown = fresh[:_PUSH_LIMIT]
        lines = [f"· 关注歌手新歌（{name}）"]
        for i, s in enumerate(shown, 1):
            lines.append(f"  {i}. {s.get('name')} - {s.get('artist')}")
        # 只并集本歌手命中的 sid，且只记**已推送**的条目：把未推送的第
        # _PUSH_LIMIT 首之后的歌也记 seen，它们永远不会出现在任何推送里
        # （seen 上限 200，普通用户挤不掉），等于静默丢歌
        sids = list(dict.fromkeys(s["sid"] for s in shown))
        return "\n".join(lines), sids

    async def _fetch_followed_songs(self) -> list[dict]:
        """取一次「关注的歌手新歌」，失败返回空列表（各歌手跳过推送）。"""
        try:
            return await self._service.client_of(SOURCE_KG).followed_new_songs(30)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 歌手新歌获取失败：{e}")
            return []

    def describe(self, umo: str) -> str:
        row = self._subs.get(umo)
        if not row:
            return "当前会话没有订阅。可用：订阅 日推 / 订阅新歌 歌手名"
        lines = []
        lines.append("· 日推（网易云，每天推送）" if row.get("daily") else "· 日推：未订阅")
        if row.get("artists"):
            lines.append("· 歌手新歌：" + "、".join(a.get("name", "") for a in row["artists"]))
        else:
            lines.append("· 歌手新歌：未订阅（订阅新歌 歌手名，需酷狗登录）")
        return "\n".join(lines)

    def source_note(self) -> str:
        return f"日推来源：{SOURCE_NAMES[SOURCE_NCM]} · 新歌来源：{SOURCE_NAMES[SOURCE_KG]}"
