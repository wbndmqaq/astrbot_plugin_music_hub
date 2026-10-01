"""群点歌台：跨用户排队队列（内存态，重启清空）。

- 播放不依赖触发事件：从会话注册表（service.umo_of）取 unified_msg_origin，
  经 core.remote.send_audio_to 主动发送
- 播完一首按歌曲时长 + 间隔自动接下一首（上限 15 分钟，防超长音频卡队列）
- 切歌 = 取消当前播放任务并立刻起下一首；清空 = 取消并清空
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from . import SOURCE_NAMES

TAG = "[music_hub]"
MAX_QUEUE = 30
_GAP_SEC = 3
_MAX_WAIT_SEC = 900


class QueueManager:
    def __init__(self, service):
        self._service = service
        self._queues: dict[str, dict] = {}

    def _q(self, scope: str) -> dict:
        return self._queues.setdefault(scope, {"items": [], "task": None, "current": None})

    def _umo_of(self, scope: str) -> str:
        return self._service.umo_of(scope)

    # ──────────── 对外 ────────────
    async def add(self, scope: str, song: dict, requester: str) -> int:
        """入队；返回位置（从 1 起）。队首歌立即开始播放。"""
        q = self._q(scope)
        if len(q["items"]) >= MAX_QUEUE:
            return -1
        q["items"].append({"song": song, "by": requester})
        pos = len(q["items"])
        if q["task"] is None or q["task"].done():
            if not self._umo_of(scope):
                q["items"].pop()
                return -2
            q["task"] = asyncio.create_task(self._player(scope))
        return pos

    def items(self, scope: str) -> list[dict]:
        return list(self._q(scope)["items"])

    def current(self, scope: str) -> dict | None:
        return self._q(scope)["current"]

    def skip(self, scope: str) -> dict | None:
        """切歌：终止当前播放，返回即将播放的下一首（无则 None）。"""
        q = self._q(scope)
        if q["task"] and not q["task"].done():
            q["task"].cancel()
        q["task"] = None
        nxt = q["items"][0] if q["items"] else None
        if nxt is not None and self._umo_of(scope):
            q["task"] = asyncio.create_task(self._player(scope))
        return nxt

    def clear(self, scope: str) -> int:
        q = self._queues.get(scope)
        if not q:
            return 0
        n = len(q["items"])
        if q["task"] and not q["task"].done():
            q["task"].cancel()
        q["items"].clear()
        q["current"] = None
        q["task"] = None
        return n

    # ──────────── 播放循环 ────────────
    async def _player(self, scope: str) -> None:
        q = self._q(scope)
        me = asyncio.current_task()
        try:
            while q["items"]:
                entry = q["items"].pop(0)
                q["current"] = entry
                song = entry["song"]
                try:
                    play = await self._service.resolve_play(song)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 单首失败跳过继续
                    logger.warning(f"{TAG} 点歌台取流失败，跳过：{e}")
                    await self._service.send_to_scope(scope, f"⚠ 点歌台跳过「{song.get('name')}」：取流失败")
                    continue
                result = await self._service.send_audio(scope, song, play, note="点歌台")
                self._service.stats.record(
                    song.get("source", ""),
                    "play",
                    ok=bool(result.get("ok")),
                    detail=song.get("name", "")[:40],
                )
                if not result.get("ok"):
                    await self._service.send_to_scope(scope, f"⚠ 点歌台发送「{song.get('name')}」失败，跳过")
                if play.get("trial"):
                    dur = 60 + _GAP_SEC  # 试听片段只有 60s，按实际时长等待
                else:
                    dur = int((song.get("dtMs") or 0) / 1000) + _GAP_SEC
                await asyncio.sleep(min(max(dur, 10), _MAX_WAIT_SEC))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 点歌台播放循环异常（{scope}）：{e}")
        finally:
            # 只有「自己仍是登记中的任务」时才清理，防止切歌新任务的引用被旧任务清掉
            if q.get("task") is me:
                q["current"] = None
                q["task"] = None

    def describe(self, scope: str) -> str:
        """队列文本（聊天端 `队列` 指令）。"""
        q = self._queues.get(scope)
        if not q or (not q["items"] and not q["current"]):
            return "点歌台队列是空的，发「排队 关键词」加入 ♪"
        lines = []
        cur = q["current"]
        if cur:
            lines.append(f"▶ 正在播：{cur['song'].get('name')} - {cur['song'].get('artist')}（{cur['by']}）")
        for i, e in enumerate(q["items"], 1):
            src = SOURCE_NAMES.get(e["song"].get("source"), "")
            lines.append(f"{i}. {e['song'].get('name')} - {e['song'].get('artist')}（{e['by']}）[{src}]")
        lines.append("切歌：回复「切歌」（管理员） · 清空：回复「清空队列」")
        return "\n".join(lines)
