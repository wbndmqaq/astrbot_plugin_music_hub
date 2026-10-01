"""播放历史与二段式分页缓存（内存态，重启清空）。

- history：scope → 最近成功播放的歌。存完整归一化结构（去掉 raw），
  重播时音质阶梯仍然可用
- pager：scope → 歌词 / 评论的翻页数据。与歌曲列表（SessionStore）分开存，
  翻页数据不会把点歌列表从会话里挤掉；带 TTL 防陈旧

纯内存结构，不依赖 AstrBot —— 可直接单测。
"""

from __future__ import annotations

import time

# 每会话播放历史上限 / 分页缓存条目有效期（秒）
HISTORY_LIMIT = 20
PAGER_TTL = 1800.0


class HistoryStore:
    def __init__(self) -> None:
        self._history: dict[str, list] = {}
        self._pagers: dict[str, dict] = {}

    # ──────────── 播放历史 ────────────
    def record(self, scope: str, song: dict) -> None:
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
    def set_pager(self, scope: str, kind: str, data: dict) -> None:
        self._pagers[scope] = {"kind": kind, "data": data, "ts": time.time()}

    def get_pager(self, scope: str, kind: str) -> dict | None:
        entry = self._pagers.get(scope)
        if not entry or entry.get("kind") != kind:
            return None
        if time.time() - entry.get("ts", 0) > PAGER_TTL:
            self._pagers.pop(scope, None)
            return None
        return entry.get("data") or {}
