"""会话存储：每个群/私聊作用域记住最近的列表（点歌结果、榜单、专辑候选…），
供 ``听N`` / ``听所有`` / 二段式操作（歌词/评论 N）消费。

- 内存缓存（TTL + 容量上限）+ AstrBot KV 持久化（重载不丢列表）
- 写入时更新共享仲裁文件 ``data/plugin_data/_music_session_owner.json``，
  与同目录其他音乐插件约定：裸 ``听N`` 只由「最近产出列表的插件」响应。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from . import PLUGIN_NAME

TTL = 600.0
MAX_ENTRIES = 512
OWNER_FILE_MAX_SCOPES = 256


def scope_of(platform: str, group_id: str, sender_id: str) -> str:
    return f"{platform}:{group_id or sender_id}"


class SessionStore:
    def __init__(self, plugin, data_dir: Path):
        self._plugin = plugin
        self._kv_prefix = "mh"
        self._lock = asyncio.Lock()
        self._cache: dict[str, dict] = {}
        # 共享仲裁文件放 data/plugin_data/（与同目录其他音乐插件约定的旧位置）
        self._owner_path = data_dir.parent / "_music_session_owner.json"

    # ──────────── KV ────────────
    async def _kv_get(self, key: str, default=None):
        try:
            return await self._plugin.get_kv_data(key, default)
        except Exception:
            return default

    async def _kv_put(self, key: str, value) -> None:
        try:
            await self._plugin.put_kv_data(key, value)
        except Exception:
            pass

    # ──────────── 仲裁 ────────────
    def _claim_owner(self, scope: str) -> None:
        """同步实现（线程池里跑）：标记本插件为该 scope 的最近音乐插件（老约定，供裸 #听N 仲裁）。"""
        try:
            data = {}
            if self._owner_path.exists():
                data = json.loads(self._owner_path.read_text("utf-8"))
            scopes = data.get("scopes", {}) if isinstance(data.get("scopes"), dict) else {}
            scopes[scope] = {"plugin": PLUGIN_NAME, "ts": int(time.time())}
            if len(scopes) > OWNER_FILE_MAX_SCOPES:
                scopes = dict(
                    sorted(
                        scopes.items(), key=lambda kv: -(kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0)
                    )[: OWNER_FILE_MAX_SCOPES // 2]
                )
            data["version"] = 2
            data["scopes"] = scopes
            tmp = self._owner_path.with_suffix(f".{PLUGIN_NAME}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(self._owner_path)
        except (OSError, ValueError):
            pass

    async def claim_owner(self, scope: str) -> None:
        """事件循环安全版：文件读写在 to_thread。"""
        await asyncio.to_thread(self._claim_owner, scope)

    def _owns_scope_impl(self, scope: str) -> bool:
        """裸 ``听N`` 仲裁：无文件/无记录/超时视为无主（放行）。"""
        try:
            if not self._owner_path.exists():
                return True
            data = json.loads(self._owner_path.read_text("utf-8"))
            entry = (data.get("scopes", {}) or {}).get(scope)
            if not isinstance(entry, dict) or not entry.get("plugin"):
                return True
            if time.time() - float(entry.get("ts", 0)) > TTL:
                return True
            return entry.get("plugin") == PLUGIN_NAME
        except (OSError, ValueError, TypeError):
            return True

    async def owns_scope(self, scope: str) -> bool:
        """事件循环安全版：文件读在 to_thread。"""
        return await asyncio.to_thread(self._owns_scope_impl, scope)

    # ──────────── 会话 ────────────
    async def set(self, scope: str, kind: str, data: dict, action: str = "") -> None:
        """记录一个会话。``data`` 通常含 keyword/songs 等；strip 掉 raw 防膨胀。"""
        entry = {
            "kind": kind,
            "keyword": data.get("keyword", ""),
            "data": {k: v for k, v in data.items() if k != "raw"},
            "action": action,
            "updatedAt": time.time(),
        }
        async with self._lock:
            self._cache[scope] = entry
            if len(self._cache) > MAX_ENTRIES:
                # 批量淘汰：只弹一条的话高频写入下 _cache 会远超上限，内存不受控
                over = len(self._cache) - MAX_ENTRIES + 1
                for k in sorted(self._cache, key=lambda k: self._cache[k].get("updatedAt", 0))[:over]:
                    self._cache.pop(k, None)
            # KV 写放在锁内：同一 scope 的并发 set 若在锁外落盘，
            # 后完成的旧 entry 可能覆盖新 entry，重载后「听N」拿到过期列表。
            await self._kv_put(f"{self._kv_prefix}:sess:{scope}", entry)
        await self.claim_owner(scope)

    async def get(self, scope: str, refresh: bool = False) -> dict | None:
        async with self._lock:
            entry = self._cache.get(scope)
            if entry and time.time() - entry.get("updatedAt", 0) < TTL:
                if refresh:
                    entry["updatedAt"] = time.time()
                return dict(entry)
        stored = await self._kv_get(f"{self._kv_prefix}:sess:{scope}")
        if isinstance(stored, dict) and stored.get("kind"):
            if time.time() - float(stored.get("updatedAt", 0)) < TTL:
                async with self._lock:
                    # 二次检查：等 KV 期间可能已有别的协程写入了更新的 entry，
                    # 无条件覆盖会让新数据被旧值顶掉。
                    current = self._cache.get(scope)
                    if current is not None and current.get("updatedAt", 0) >= stored.get("updatedAt", 0):
                        return dict(current)
                    self._cache[scope] = stored
                return dict(stored)
        return None

    async def update_action(self, scope: str, action: str) -> None:
        entry = await self.get(scope, refresh=True)
        if entry:
            entry["action"] = action
            await self.set(scope, entry.get("kind", "songs"), entry.get("data", {}), action=action)

    async def clear_action(self, scope: str) -> None:
        entry = await self.get(scope)
        if entry and entry.get("action"):
            await self.set(scope, entry["kind"], entry.get("data", {}), action="")

    async def clear(self, scope: str) -> None:
        async with self._lock:
            self._cache.pop(scope, None)
        try:
            await self._plugin.delete_kv_data(f"{self._kv_prefix}:sess:{scope}")
        except Exception:
            pass

    async def songs_of(self, scope: str) -> list[dict]:
        entry = await self.get(scope)
        if not entry:
            return []
        data = entry.get("data", {})
        songs = data.get("songs")
        return songs if isinstance(songs, list) else []

    async def close(self) -> None:
        self._cache.clear()
