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
# 上游缺时长信息时的估算值（多数流行歌 3~4 分钟）
_FALLBACK_DUR_SEC = 180


def wait_seconds(song: dict, play: dict) -> float:
    """本曲播完后再等多久接下一首（含间隔，已夹在合理区间内）。

    dtMs 是各源归一化时的可选字段，缺失时不能退化成下限 10 秒 ——
    那与真实时长无关，会让「30 首连播」变成 30 次 10 秒空转。
    """
    if play.get("trial"):
        dur = 60 + _GAP_SEC  # 试听片段只有 60s
    elif song.get("dtMs"):
        dur = int(song["dtMs"] / 1000) + _GAP_SEC
    else:
        dur = _FALLBACK_DUR_SEC + _GAP_SEC
    return min(max(dur, 10), _MAX_WAIT_SEC)


class RequestDesk:
    def __init__(self, service):
        self._service = service
        self._queues: dict[str, dict] = {}
        # 每个 scope 一把锁：skip/clear 的 _abort 内部 await gather，是 await 点，
        # 并发「切歌」各自恢复后会互相覆盖 q["task"]，导致两个播放器同时播出。
        # 只给 skip/clear/add 用，_player 绝不能碰（_abort 持锁等播放任务退出，
        # 播放任务若也等这把锁就是死锁）。
        self._locks: dict[str, asyncio.Lock] = {}

    def _q(self, scope: str) -> dict:
        return self._queues.setdefault(scope, {"items": [], "task": None, "current": None})

    def _lock_for(self, scope: str) -> asyncio.Lock:
        return self._locks.setdefault(scope, asyncio.Lock())

    def _reclaim_scope(self, scope: str) -> None:
        """空 scope 的队列与锁条目一并回收：不回收的话每个用过的群各留一份，永久驻留。

        仅在持 scope 锁或播放任务自然结束时调用（见 _player finally 的注释）。"""
        q = self._queues.get(scope)
        if q and not q["items"] and q["current"] is None:
            self._queues.pop(scope, None)
            self._locks.pop(scope, None)

    def _umo_of(self, scope: str) -> str:
        return self._service.umo_of(scope)

    def _start_or_none(self, scope: str, q: dict):
        """起播放任务，失败返回 None（不回滚入队——调用方决定）。

        走 service.spawn 以纳入统一生命周期管理——否则裸 create_task 在插件
        卸载/重载后仍存活，继续向已失效的 umo 发消息。spawn 在超过并发上限时
        抛 TooManyTasks，调用方必须回滚已入队的条目。
        """
        try:
            return self._service.spawn(self._player(scope))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 点歌台启动播放失败（{scope}）：{e}")
            return None

    # ──────────── 对外 ────────────
    async def add(self, scope: str, song: dict, requester: str) -> int:
        """入队；返回位置（从 1 起）。队首歌立即开始播放。

        同样持 scope 锁：虽无 await 点，但不持锁的话会在 skip 的 _abort 等待窗口
        里看到 done 的旧任务并起播、写入 q["task"]，随后被 _abort 收尾的置 None
        覆盖掉登记，队列从此指向不到正在播放的任务。
        """
        async with self._lock_for(scope):
            q = self._q(scope)
            if len(q["items"]) >= MAX_QUEUE:
                return -1
            q["items"].append({"song": song, "by": requester})
            pos = len(q["items"])
            if q["task"] is None or q["task"].done():
                task = self._start_or_none(scope, q)
                if task is None:
                    # 启动失败必须回滚：否则这首歌永远滞留在队列里，
                    # 既不播放也会被 describe() 误标成「排队中」
                    q["items"].pop()
                    return -3
                q["task"] = task
            return pos

    def items(self, scope: str) -> list[dict]:
        return list(self._q(scope)["items"])

    def current(self, scope: str) -> dict | None:
        return self._q(scope)["current"]

    async def skip(self, scope: str) -> dict | None:
        """切歌：终止当前播放并等待其真正退出，返回即将播放的下一首（无则 None）。

        必须 await 旧任务：cancel() 只投递信号，不等它落地就起新任务会让
        正在发送的语音被截断、同时下一首已经开始播（两首重叠）。
        主体全程持 scope 锁：_abort 的 gather 是 await 点，不持锁时两个并发
        切歌各自恢复后会互相覆盖 q["task"]，两个播放器并发播出。
        """
        async with self._lock_for(scope):
            q = self._q(scope)
            await self._abort(q)
            nxt = q["items"][0] if q["items"] else None
            if nxt is not None and self._umo_of(scope):
                # 启动失败时保持 task=None：队列仍可再 skip/clear，不至于彻底停摆；
                # 返回 None 让 handler 提示「启动失败」而不是对着没播的歌播报「接下来」
                q["task"] = self._start_or_none(scope, q)
                if q["task"] is None:
                    return None
            return nxt

    async def clear(self, scope: str) -> int:
        async with self._lock_for(scope):
            q = self._queues.get(scope)
            if not q:
                return 0
            n = len(q["items"])
            await self._abort(q)
            q["items"].clear()
            q["current"] = None
            # 持锁状态下回收空 scope：等待方仍持有旧锁引用，新来的会重建，无碍
            self._reclaim_scope(scope)
            return n

    @staticmethod
    async def _abort(q: dict) -> None:
        """取消当前播放任务并等它真正退出。

        task 引用在 gather **之后**才置 None：_player 的 finally 靠
        ``q["task"] is me`` 判断自己是否仍是登记中的任务，先置 None 会让旧任务
        的 finally 跳过状态清理（current 残留、误清掉新任务的引用）。
        """
        task = q.get("task")
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        q["task"] = None

    async def stop_all(self) -> None:
        """插件卸载时取消所有会话的播放任务，避免重载后继续向失效会话推送。"""
        tasks = []
        for q in self._queues.values():
            t = q.get("task")
            if t and not t.done():
                t.cancel()
                tasks.append(t)
            q["task"] = None
            q["current"] = None
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._queues.clear()

    # ──────────── 播放循环 ────────────
    async def _player(self, scope: str) -> None:
        q = self._q(scope)
        me = asyncio.current_task()
        cancelled = False
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
                await asyncio.sleep(wait_seconds(song, play))
        except asyncio.CancelledError:
            # 切歌/清空/卸载的取消：收尾交给 _abort / stop_all，这里不做条目回收
            cancelled = True
            raise
        except Exception as e:  # noqa: BLE001
            # 非预期异常原先只记日志：队列静默停摆，群里没人知道为什么没歌了
            logger.warning(f"{TAG} 点歌台播放循环异常（{scope}）：{e}")
            try:
                remain = len(q["items"])
                if remain:
                    await self._service.send_to_scope(
                        scope, f"⚠ 点歌台异常暂停，还有 {remain} 首未播，回复「切歌」重新开始"
                    )
            except Exception:  # noqa: BLE001 - 提示发不出去也不再连锁
                pass
        finally:
            # 只有「自己仍是登记中的任务」时才清理，防止切歌新任务的引用被旧任务清掉
            if q.get("task") is me:
                q["current"] = None
                q["task"] = None
                # 自然播完才回收空 scope；取消路径上可能有 clear/skip 正持锁等本任务
                # 退出，此时动 _locks 会让并发方各持不同锁对象，互斥失效
                if not cancelled and not q["items"]:
                    self._reclaim_scope(scope)

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
