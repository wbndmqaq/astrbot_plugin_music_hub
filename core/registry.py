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
        # 并发落盘合并：群内多人同时点歌会 spawn 多个 save()，
        # 不合并则后写完的任务可能带着更旧的数据覆盖更新的
        self._saving = False
        self._dirty = False

    async def load(self) -> None:
        """启动时从 KV 恢复（加载失败的条目直接丢弃）。"""
        data = await self._kv_get(self._key, None)
        if isinstance(data, dict):
            self._rows = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("umo")}

    def note(self, scope: str, umo: str) -> bool:
        """登记会话的 umo。返回 True 表示落盘内容会变化（首次登记或 umo 变更，
        如群迁移/平台换号），调用方应安排落盘；仅活跃时间戳刷新不触发。

        ts 每次活跃都刷新：save() 按 ts 倒序截断，不刷新会让长期活跃的会话
        被钉在首次联系时刻，从而优先于真正陈旧的会话被裁掉。
        """
        if not umo:
            return False
        row = self._rows.get(scope)
        changed = row is None or row.get("umo") != umo
        self._rows[scope] = {"umo": umo, "ts": int(time.time())}
        # 保存进行中登记了新会话：置脏标记，让 save() 的尾随循环补写一次，
        # 否则 await 期间的变更永远等不到下一次 save
        if self._saving:
            self._dirty = True
        return changed

    async def save(self) -> None:
        """按最近活跃截断后落盘。并发调用合并为一次，尾随调用保证最终一致。"""
        if self._saving:
            self._dirty = True
            return
        self._saving = True
        try:
            while True:
                self._dirty = False
                trimmed = dict(sorted(self._rows.items(), key=lambda kv: -kv[1].get("ts", 0))[:MAX_SCOPES])
                await self._kv_put(self._key, trimmed)
                if not self._dirty:
                    return
        finally:
            self._saving = False
            # 退出前又有新登记（发生在上面的 _dirty 判定之后、finally 置位之前）：
            # 不补跑一次这些 umo 就永远不落盘，重启后点歌台/订阅推送会丢目标会话。
            if self._dirty:
                self._saving = False
                await self.save()

    def umo_of(self, scope: str) -> str:
        return (self._rows.get(scope) or {}).get("umo", "")

    def rows(self) -> list[dict]:
        out = [{"scope": k, **v} for k, v in self._rows.items()]
        return sorted(out, key=lambda r: -r.get("ts", 0))
