"""会话存储：每个群/私聊作用域记住最近的列表（点歌结果、榜单、专辑候选…），
供 ``听N`` / ``听所有`` / 二段式操作（歌词/评论 N）消费。

- 内存缓存（TTL + 容量上限）+ AstrBot KV 持久化（重载不丢列表）
- 写入时更新共享仲裁文件 ``data/plugin_data/_music_session_owner.json``，
  与同目录其他音乐插件约定：裸 ``听N`` 只由「最近产出列表的插件」响应。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from astrbot.api import logger

from . import PLUGIN_NAME

TTL = 600.0
MAX_ENTRIES = 512
OWNER_FILE_MAX_SCOPES = 256
# KV 过期清扫的登记表上限：只覆盖本进程写过的 scope，超过即触发一次清扫
_KV_SCOPE_TRACK_MAX = 1024


def scope_of(platform: str, group_id: str, sender_id: str) -> str:
    return f"{platform}:{group_id or sender_id}"


class SessionStore:
    def __init__(self, plugin, data_dir: Path):
        self._plugin = plugin
        self._kv_prefix = "mh"
        # _lock 只护缓存结构（持有期间绝无 await）；KV 写的串行化由 per-scope
        # 锁承担（见 set/mutate）——全局锁内 await KV 写会把所有会话的写操作
        # 在磁盘抖动/大 JSON 时串成一条队
        self._lock = asyncio.Lock()
        # 与 queue._locks 同思路：setdefault 无 await，单事件循环下天然原子。
        # 不回收条目（pop 会让等锁方持旧锁引用、新调用方拿新锁，同 scope 写序
        # 失守）；scope 数以活跃会话数为上界，泄漏面可忽略
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self._cache: dict[str, dict] = {}
        # KV 侧过期清扫登记表（scope -> 最近一次 KV 写时间）：AstrBot KV 无枚举
        # API，过期键只有恰好被读到才按「无会话」处理，文件里的键只增不删（每条
        # 含完整 songs 列表，可达数 KB）。登记表有界，把常驻进程的增长截断；
        # 跨重载的存量键不可枚举属已知局限
        self._kv_scopes: dict[str, float] = {}
        # 共享仲裁文件放 data/plugin_data/（与同目录其他音乐插件约定的旧位置）
        self._owner_path = data_dir.parent / "_music_session_owner.json"

    def _scope_lock(self, scope: str) -> asyncio.Lock:
        return self._scope_locks.setdefault(scope, asyncio.Lock())

    # ──────────── KV ────────────
    async def _kv_get(self, key: str, default=None):
        try:
            return await self._plugin.get_kv_data(key, default)
        except Exception:
            return default

    async def _kv_put(self, key: str, value) -> None:
        try:
            await self._plugin.put_kv_data(key, value)
        except Exception as e:
            # 吞掉但不无声：会话列表落盘失败若无痕，重载后「列表莫名丢了」无法排查
            # （对齐 service.put_kv 的「吞掉但有日志」纪律）
            logger.warning(f"[music_hub] 会话 KV 写入失败（{key}）：{e}")

    async def _kv_delete(self, key: str) -> None:
        try:
            await self._plugin.delete_kv_data(key)
        except Exception as e:
            logger.debug(f"[music_hub] 会话 KV 清扫删除失败（{key}）：{e}")

    async def _kv_sweep(self) -> None:
        """登记表超限时删掉已过 TTL 且缓存无活跃条目的 scope 的 KV 键。

        在 set() 的 scope 锁外调用（sweep 要取其他 scope 的锁，scope 锁不可重入）。
        缓存里仍新鲜的条目跳过（get(refresh=True) 只续内存 TTL、登记表时间是最后
        一次 KV 写，单看登记表会误删活跃会话）；并发窗口用 scope 锁 + 删除前二次
        校验兜住。登记表全新鲜仍超界时只丢最旧登记不删 KV，保持有界。"""
        now = time.time()
        async with self._lock:
            candidates = [s for s, ts in self._kv_scopes.items() if now - ts >= TTL]
        for s in candidates:
            async with self._scope_lock(s):
                async with self._lock:
                    if s not in self._kv_scopes:
                        continue  # 已被并发 sweep 处理
                    entry = self._cache.get(s)
                    if entry and time.time() - entry.get("updatedAt", 0) < TTL:
                        continue  # 缓存仍活跃：留着不删
                    self._kv_scopes.pop(s, None)
                await self._kv_delete(f"{self._kv_prefix}:sess:{s}")
        if len(self._kv_scopes) > _KV_SCOPE_TRACK_MAX:
            async with self._lock:
                over = len(self._kv_scopes) - _KV_SCOPE_TRACK_MAX
                for s, _t in sorted(self._kv_scopes.items(), key=lambda kv: kv[1])[:over]:
                    self._kv_scopes.pop(s, None)

    # ──────────── 仲裁 ────────────
    def _claim_owner(self, scope: str) -> None:
        """同步实现（线程池里跑）：标记本插件为该 scope 的最近音乐插件（老约定，供裸 #听N 仲裁）。

        并发 claim（多群同时出列表）的读改写竞态是已知问题：两个写者可能互相
        覆盖对方的 scope 记录。影响可接受——只是仲裁文件的即时快照短暂缺一条，
        该会话的下一条消息会重新 claim 收敛，不值得为此加锁。"""
        try:
            data = {}
            if self._owner_path.exists():
                data = json.loads(self._owner_path.read_text("utf-8"))
            # 仲裁文件由多个插件共写，schema 不可控：根是 list/str 时 .get 直接
            # AttributeError，不在下方 except 捕获面里，会炸出 claim_owner
            if not isinstance(data, dict):
                data = {}
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
            # tmp 名带随机后缀：固定名在并发 claim（多群同时出列表）时
            # 两个写入者互踩同一个 tmp 文件，先完成的 replace 会被后写的破坏
            tmp = self._owner_path.with_suffix(f".{PLUGIN_NAME}.{os.urandom(4).hex()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            tmp.replace(self._owner_path)
        except (OSError, ValueError):
            pass

    async def claim_owner(self, scope: str) -> None:
        """事件循环安全版：文件读写在 to_thread。"""
        await asyncio.to_thread(self._claim_owner, scope)

    def _sweep_owner_tmp(self) -> None:
        # 写仲裁文件中途失败（磁盘满/权限）残留的孤儿 tmp。只清带本插件名后缀的：
        # 该文件与同目录其他音乐插件共写，别人的 tmp 不能动
        try:
            for p in self._owner_path.parent.glob(f"{self._owner_path.stem}.{PLUGIN_NAME}.*.tmp"):
                p.unlink(missing_ok=True)
        except OSError:
            pass

    async def sweep_owner_tmp(self) -> None:
        await asyncio.to_thread(self._sweep_owner_tmp)

    def _owns_scope_impl(self, scope: str) -> bool:
        """裸 ``听N`` 仲裁：无文件/无记录/超时视为无主（放行）。"""
        try:
            if not self._owner_path.exists():
                return True
            data = json.loads(self._owner_path.read_text("utf-8"))
            if not isinstance(data, dict):
                return True
            scopes = data.get("scopes", {})
            entry = scopes.get(scope) if isinstance(scopes, dict) else None
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
    @staticmethod
    def _normalize_stored(stored: dict) -> dict:
        """KV 读回的 entry 归一化：updatedAt 统一成 float。

        脏数据（手改 JSON / 旧版本写坏 updatedAt，含数字字符串）若原样入缓存，
        缓存命中路径 ``time.time() - str`` 会 TypeError——归一必须发生在入缓存之前，
        而不是只在读取时算一次局部变量。
        """
        norm = dict(stored)
        try:
            norm["updatedAt"] = float(stored.get("updatedAt", 0))
        except (TypeError, ValueError):
            norm["updatedAt"] = 0.0
        return norm

    async def set(self, scope: str, kind: str, data: dict, action: str = "") -> None:
        """记录一个会话。``data`` 通常含 keyword/songs 等；strip 掉 raw 防膨胀。"""
        entry = {
            "kind": kind,
            "keyword": data.get("keyword", ""),
            "data": {k: v for k, v in data.items() if k != "raw"},
            "action": action,
            "updatedAt": time.time(),
        }
        async with self._scope_lock(scope):
            async with self._lock:
                self._cache[scope] = entry
                if len(self._cache) > MAX_ENTRIES:
                    # 批量淘汰：只弹一条的话高频写入下 _cache 会远超上限，内存不受控
                    over = len(self._cache) - MAX_ENTRIES + 1
                    for k in sorted(self._cache, key=lambda k: self._cache[k].get("updatedAt", 0))[:over]:
                        self._cache.pop(k, None)
            # KV 写按 scope 串行（同 scope 的并发 set 若在锁外落盘，后完成的旧
            # entry 可能覆盖新 entry，重载后「听N」拿到过期列表）；全局锁已退出
            # ——KV 慢时只堵同会话的写操作，其他会话不再被连带排队
            await self._kv_put(f"{self._kv_prefix}:sess:{scope}", entry)
            # 登记在全局锁内做（同步段）；清扫留到 scope 锁释放后（_kv_sweep 要取
            # 其他 scope 的锁，asyncio.Lock 不可重入，在锁内调会自死锁）
            async with self._lock:
                self._kv_scopes[scope] = entry["updatedAt"]
                need_sweep = len(self._kv_scopes) > _KV_SCOPE_TRACK_MAX
        await self.claim_owner(scope)
        if need_sweep:
            await self._kv_sweep()

    async def get(self, scope: str, refresh: bool = False) -> dict | None:
        async with self._lock:
            entry = self._cache.get(scope)
            if entry and time.time() - entry.get("updatedAt", 0) < TTL:
                if refresh:
                    # refresh 只续内存 TTL：KV 里的 updatedAt 不回写。跨重载（内存
                    # 缓存清空）后以落盘时间为准，活跃会话的剩余 TTL 会偏短——
                    # 属有意为之的近似，避免活跃会话频繁回写 KV
                    entry["updatedAt"] = time.time()
                return dict(entry)
        stored = await self._kv_get(f"{self._kv_prefix}:sess:{scope}")
        if isinstance(stored, dict) and stored.get("kind"):
            try:
                stored_ts = float(stored.get("updatedAt", 0))
            except (TypeError, ValueError):
                # 脏 KV（手改 JSON / 旧版本写坏 updatedAt，含数字字符串）：按「缓存
                # 过期」处理走重取而不冒泡——与 _owns_scope_impl 对落盘数据的防御一致
                stored_ts = 0.0
            fresh = time.time() - stored_ts < TTL
            if fresh:
                async with self._lock:
                    # 二次检查：等 KV 期间可能已有别的协程写入了更新的 entry，
                    # 无条件覆盖会让新数据被旧值顶掉。比较用归一后的 float：
                    # 裸 stored["updatedAt"] 是字符串时 float >= str 会 TypeError
                    current = self._cache.get(scope)
                    if current is not None and current.get("updatedAt", 0) >= stored_ts:
                        return dict(current)
                    norm = self._normalize_stored(stored)
                    self._cache[scope] = norm
                return dict(norm)
        return None

    @staticmethod
    def _empty_entry() -> dict:
        """create=True 时的最小会话骨架：kind 与 data 键位对齐 set() 的结构，
        让消费方（take_action_target / switch_source）对空数据走既有「无列表」分支。"""
        return {"kind": "play", "keyword": "", "data": {}, "action": "", "updatedAt": 0.0}

    async def mutate(self, scope: str, mutator, create: bool = False) -> dict | None:
        """原子读改写：mutator(entry) 就地修改，落盘后返回副本。

        get→set 的整包回写在两协程交错时会把刚写入的新列表用旧数据顶掉
        （「点歌 新词」与「听N收尾/记播放」并发，缓存与 KV 双写竞态）——
        凡只改 entry 局部字段的调用方一律走这里，不做整包回写。
        缓存未命中时与 get() 同语义地从 KV 装载（含归一与过期判断），无会话返回 None。
        create=True 时无会话不返回 None，而是构造最小骨架让 mutator 落笔
        （play_song 直播路径记 lastPlayed：直接「播放 关键词」从不建会话，
        否则随后「换源」误报「还没有播放过歌曲」）。

        锁纪律：mutator 与 KV 写必须对同 scope 串行（两个并发「听N」各取到
        同一个 pending_action 并重复执行动作的竞态正是靠这把 scope 锁挡住的）。
        mutator 在全局 _lock 的同步段内执行——它做的是在位突变，并发 get() 的
        浅拷贝必须看到突变前/后的完整状态而不是半截；KV 写在 scope 锁内、全局
        锁外——同 scope 写有序，跨 scope 不再被磁盘抖动串行。只有缓存未命中的
        KV **读**放锁外——读不改共享状态。
        """
        async with self._scope_lock(scope):
            entry = self._cache.get(scope)
            fresh_entry = None
            if not (entry is not None and time.time() - entry.get("updatedAt", 0) < TTL):
                stored = await self._kv_get(f"{self._kv_prefix}:sess:{scope}")
                if isinstance(stored, dict) and stored.get("kind"):
                    fresh_entry = self._normalize_stored(stored)
                    if time.time() - fresh_entry.get("updatedAt", 0) >= TTL:
                        fresh_entry = None
                if fresh_entry is None:
                    if not create:
                        return None
                    fresh_entry = self._empty_entry()
            async with self._lock:
                # 二次检查：锁外读 KV 期间可能已有别的协程写入更新的 entry，
                # 无条件覆盖会让新数据被旧值顶掉（归一后 float 比较，字符串时间戳不炸）
                if fresh_entry is not None:
                    current = self._cache.get(scope)
                    entry = (
                        current
                        if current is not None and time.time() - current.get("updatedAt", 0) < TTL
                        else fresh_entry
                    )
                    self._cache[scope] = entry
                else:
                    entry = self._cache.get(scope) or entry
                mutator(entry)
                entry["updatedAt"] = time.time()
            # KV 写放 scope 锁内、全局锁外（同步段已完成突变，get() 在写盘期间
            # 读到的是完整新状态）
            await self._kv_put(f"{self._kv_prefix}:sess:{scope}", entry)
            # 与 set() 同口径登记（清扫触发集中在 set()，这里只保登记表完整）
            async with self._lock:
                self._kv_scopes[scope] = entry["updatedAt"]
            return dict(entry)

    async def update_action(self, scope: str, action: str) -> None:
        await self.mutate(scope, lambda e: e.__setitem__("action", action))

    async def update_action_if_keyword(self, scope: str, action: str, expect_keyword: str) -> bool:
        """keyword 仍匹配时才写回动作（锁内 CAS）。

        「歌词 晴天 → 听1」撞上冷却后动作要写回，但写回窗口里他人「点歌 新词」
        可能已整包替换列表——无条件下旧列表的意图会落到新列表上，下一个
        「听1」行为错位。比对消费时的列表关键词，替换过的新列表不接旧动作。"""
        ok = {"v": False}

        def _mut(entry: dict) -> None:
            if (entry.get("keyword") or "") == (expect_keyword or ""):
                entry["action"] = action
                ok["v"] = True

        await self.mutate(scope, _mut)
        return ok["v"]

    async def songs_of(self, scope: str) -> list[dict]:
        entry = await self.get(scope)
        if not entry:
            return []
        data = entry.get("data", {})
        songs = data.get("songs")
        return songs if isinstance(songs, list) else []

    async def close(self) -> None:
        self._cache.clear()
