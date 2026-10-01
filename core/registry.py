"""会话注册表：scope → unified_msg_origin 的映射与持久化。

点歌台队列 / 订阅推送 / WebUI 远程投递都要在「没有事件对象」的上下文里
主动发消息，前提是知道目标会话的 umo。任何 handler 收到消息时都会经
``note()`` 登记，本模块负责去重、容量截断与 KV 落盘。

纯内存 + 两个 KV 回调，不依赖 AstrBot —— 可直接单测。
"""

from __future__ import annotations

import time

# 持久化容量：只留最近活跃的会话（订阅 / 队列远多于它的场景罕见）
MAX_SCOPES = 256


class UmoRegistry:
    def __init__(self, kv_get, kv_put, key: str = "umo_map"):
        self._kv_get = kv_get  # async (key, default) -> value
        self._kv_put = kv_put  # async (key, value) -> None
        self._key = key
        self._rows: dict[str, dict] = {}

    async def load(self) -> None:
        """启动时从 KV 恢复（加载失败的条目直接丢弃）。"""
        data = await self._kv_get(self._key, None)
        if isinstance(data, dict):
            self._rows = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("umo")}

    def note(self, scope: str, umo: str) -> bool:
        """登记会话的 umo。返回 True 表示落盘内容会变化（首次登记或 umo 变更，
        如群迁移/平台换号），调用方应安排落盘；仅活跃时间戳刷新不触发。"""
        if not umo:
            return False
        row = self._rows.get(scope)
        if row and row.get("umo") == umo:
            return False
        changed = row is None or row.get("umo") != umo
        self._rows[scope] = {"umo": umo, "ts": int(time.time())}
        return changed

    async def save(self) -> None:
        """按最近活跃截断后落盘。"""
        trimmed = dict(sorted(self._rows.items(), key=lambda kv: -kv[1].get("ts", 0))[:MAX_SCOPES])
        await self._kv_put(self._key, trimmed)

    def umo_of(self, scope: str) -> str:
        return (self._rows.get(scope) or {}).get("umo", "")

    def rows(self) -> list[dict]:
        out = [{"scope": k, **v} for k, v in self._rows.items()]
        return sorted(out, key=lambda r: -r.get("ts", 0))
