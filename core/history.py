"""播放历史与二段式分页缓存（内存态，重启清空）。

- history：scope → 最近成功播放的歌。存完整归一化结构（去掉 raw），
  重播时音质阶梯仍然可用
- pager：(scope, kind) → 歌词 / 评论的翻页数据。歌词与评论各占一个槽位，
  交替使用不会互相覆盖；与歌曲列表（SessionStore）分开存，翻页数据不会把
  点歌列表从会话里挤掉；带 TTL 防陈旧

纯内存结构，不依赖 AstrBot —— 可直接单测。
"""

from __future__ import annotations

import time

# 每会话播放历史上限 / 分页缓存条目有效期（秒）/ 会话总数上限
HISTORY_LIMIT = 20
PAGER_TTL = 1800.0
# scope 数量必须有上限：私聊每人一 scope，长期运行后 _history 会累积数千个 key，
# 每 key 20 条含完整 song dict 的条目，内存单调增长不回收
MAX_SCOPES = 256


class HistoryStore:
    def __init__(self) -> None:
        self._history: dict[str, list] = {}
        self._pagers: dict[tuple[str, str], dict] = {}
        self._scope_ts: dict[str, float] = {}

    def _touch(self, scope: str) -> None:
        """标记 scope 活跃并按需淘汰最旧的 scope。"""
        self._scope_ts[scope] = time.time()
        if len(self._scope_ts) <= MAX_SCOPES:
            return
        for old in sorted(self._scope_ts, key=lambda s: self._scope_ts[s])[
            : len(self._scope_ts) - MAX_SCOPES
        ]:
            self._scope_ts.pop(old, None)
            self._history.pop(old, None)
            for key in [k for k in self._pagers if k[0] == old]:
                self._pagers.pop(key, None)

    # ──────────── 播放历史 ────────────
    def record(self, scope: str, song: dict) -> None:
        self._touch(scope)
        lst = self._history.setdefault(scope, [])
        lst.append(
            {
                "name": song.get("name", ""),
                "artist": song.get("artist", ""),
                "source": song.get("source", ""),
                "ts": int(time.time()),
                "song": {k: v for k, v in song.items() if k != "raw"},
            }
        )
        del lst[:-HISTORY_LIMIT]

    def of(self, scope: str) -> list[dict]:
        return list(self._history.get(scope, []))

    def all(self, umo_of) -> list[dict]:
        """全部会话的历史汇总（umo_of：scope → umo 的回调）。"""
        out = []
        for scope, items in self._history.items():
            if items:
                out.append({"scope": scope, "umo": umo_of(scope), "items": list(items)})
        return sorted(out, key=lambda r: -(r["items"][-1]["ts"] if r["items"] else 0))

    # ──────────── 分页缓存 ────────────
    # 键是 (scope, kind)：歌词与评论各自独立翻页，互不覆盖
    def set_pager(self, scope: str, kind: str, data: dict) -> None:
        self._touch(scope)
        self._pagers[(scope, kind)] = {"data": data, "ts": time.time()}

    def get_pager(self, scope: str, kind: str) -> dict | None:
        key = (scope, kind)
        entry = self._pagers.get(key)
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > PAGER_TTL:
            self._pagers.pop(key, None)
            return None
        # 返回浅拷贝：调用方会原地改（如 detail.py 的 data["page"] = page），
        # 直接给内部引用会让一处修改静默影响另一处持有同一 dict 的代码路径
        return dict(entry.get("data") or {})

    def sweep_pagers(self) -> int:
        """清掉过期分页缓存。PAGER_TTL 原先只在读取时惰性生效，
        写入后从未再被访问的条目会一直留在内存里。"""
        now = time.time()
        stale = [k for k, v in self._pagers.items() if now - v.get("ts", 0) > PAGER_TTL]
        for k in stale:
            self._pagers.pop(k, None)
        return len(stale)
