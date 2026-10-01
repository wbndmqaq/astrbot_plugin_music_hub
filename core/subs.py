"""订阅推送：每天定时把 日推（网易云）/ 订阅歌手新歌（酷狗）推送到群。

订阅表持久化在插件 KV（key=subs_v1）：{umo: {"scope", "daily", "artists": [...]}}。
推送走 context.send_message 文本（跨平台最稳）；歌手新歌去重靠 seen hash 环。
"""

from __future__ import annotations

from astrbot.api import logger

from . import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM

TAG = "[music_hub]"
_KV_KEY = "subs_v1"
_SEEN_CAP = 200
_PUSH_LIMIT = 10


class SubManager:
    def __init__(self, service):
        self._service = service
        self._subs: dict[str, dict] = {}

    async def load(self) -> None:
        data = await self._service.get_kv("subs_v1")
        self._subs = data if isinstance(data, dict) else {}

    async def _save(self) -> None:
        await self._service.put_kv("subs_v1", self._subs)

    def _row(self, umo: str, scope: str) -> dict:
        return self._subs.setdefault(umo, {"scope": scope, "daily": False, "artists": []})

    # ──────────── 订阅管理 ────────────
    async def set_daily(self, umo: str, scope: str, on: bool) -> None:
        row = self._row(umo, scope)
        row["daily"] = on
        if not on and not row["artists"]:
            self._subs.pop(umo, None)
        await self._save()

    async def add_artist(self, umo: str, scope: str, artist: dict) -> bool:
        """artist: {id, name}。重复订阅返回 False。"""
        row = self._row(umo, scope)
        if any(a.get("id") == artist["id"] for a in row["artists"]):
            return False
        row["artists"].append({"id": str(artist["id"]), "name": artist.get("name", ""), "seen": []})
        await self._save()
        return True

    async def remove_artist(self, umo: str, scope: str, name: str) -> dict | None:
        """按名退订歌手。只做精确匹配——子串匹配会让退订「周杰伦」误删「周杰伦&费玉清」。"""
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

    def all_scopes(self) -> list[str]:
        return list(self._subs.keys())

    # ──────────── 每日推送 ────────────
    async def push_all(self) -> None:
        if not self._service.config.enable:
            return  # 总开关关闭时连订阅推送一起静默
        for umo, row in list(self._subs.items()):
            try:
                await self._push_one(umo, row)
            except Exception as e:  # noqa: BLE001 - 单会话失败不影响其他
                logger.warning(f"{TAG} 订阅推送失败（{umo}）：{e}")

    async def _push_one(self, umo: str, row: dict) -> None:
        parts = []
        if row.get("daily"):
            parts.append(await self._push_daily())
        for artist in list(row.get("artists", [])):
            part = await self._push_artist(artist)
            if part:
                parts.append(part)
        if not parts:
            return
        text = "♪ Music Hub 订阅推送\n" + "\n".join(parts) + "\n（退订：退订 日推 / 退订新歌 歌手名）"
        await self._service.send_to_umo(umo, text)

    async def _push_daily(self) -> str:
        try:
            songs = await self._service.ncm.daily_recommend()
        except Exception as e:  # noqa: BLE001
            return f"· 日推：获取失败（{e}）"
        if not songs:
            return "· 日推：今日暂无推荐（需网易云保持登录）"
        lines = [f"· 日推 TOP{min(len(songs), _PUSH_LIMIT)}"]
        for i, s in enumerate(songs[:_PUSH_LIMIT], 1):
            lines.append(f"  {i}. {s.get('name')} - {s.get('artist')}")
        return "\n".join(lines)

    async def _push_artist(self, artist: dict) -> str:
        try:
            songs = await self._service.kg.followed_new_songs(30)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 歌手新歌获取失败（{artist.get('name')}）：{e}")
            return ""
        name = artist.get("name", "")
        seen = set(artist.get("seen", []))
        fresh = [
            s
            for s in songs
            if s.get("sid") and s["sid"] not in seen and name and name in (s.get("artist") or "")
        ]
        if not fresh:
            return ""
        artist["seen"] = [s["sid"] for s in songs if s.get("sid")][-_SEEN_CAP:]
        await self._service.put_kv("subs_v1", self._subs)
        lines = [f"· 关注歌手新歌（{name}）"]
        for i, s in enumerate(fresh[:_PUSH_LIMIT], 1):
            lines.append(f"  {i}. {s.get('name')} - {s.get('artist')}")
        return "\n".join(lines)

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
