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
# 文本兜底交付（直链已送达但音频没发出去）后的接续间隔：本曲没有真实播放时长，
# 按整曲空等会把下一首拖后数分钟
_TEXT_FALLBACK_GAP_SEC = 10


def wait_seconds(song: dict, play: dict) -> float:
    """本曲播完后再等多久接下一首（含间隔，已夹在合理区间内）。

    dtMs 是各源归一化时的可选字段，缺失时不能退化成下限 10 秒 ——
    那与真实时长无关，会让「30 首连播」变成 30 次 10 秒空转。
    """
    if play.get("trial"):
        dur = 60 + _GAP_SEC  # 试听片段只有 60s
    else:
        # num() 宽松归一：上游形态漂移把 dtMs 写成字符串时，int("204000"/1000)
        # 会 TypeError——发生在歌曲已成功播出之后，被 _player 的通用 except
        # 捕获后误报「点歌台异常暂停，还有 N 首未播」
        from .api.http import num

        dur_ms = num(song.get("dtMs"))
        dur = (int(dur_ms / 1000) + _GAP_SEC) if dur_ms > 0 else (_FALLBACK_DUR_SEC + _GAP_SEC)
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
        # stop_all 置位的一次性停机闸：卸载窗口内 skip() 持锁完成 _abort 后
        # _start_or_none 会重新 spawn 引用已摘除 q dict 的僵尸播放器——
        # 在 spawn 入口拦截（stop_all 不逐 scope 取锁，靠标志位串行化）
        self._stopped = False

    def _q(self, scope: str) -> dict:
        return self._queues.setdefault(scope, {"items": [], "task": None, "current": None})

    def _lock_for(self, scope: str) -> asyncio.Lock:
        return self._locks.setdefault(scope, asyncio.Lock())

    def _reclaim_scope(self, scope: str) -> None:
        """空 scope 的队列条目回收：不回收的话每个用过的群各留一份，永久驻留。

        只回收 _queues、不动 _locks：pop 锁条目会让「等锁方持有旧锁引用、新调用方
        _lock_for 建新锁」各持不同锁对象，互斥失效——正是 per-scope 锁要防的
        双播放器并发场景。每个用过的 scope 留一把 ~100B 的 asyncio.Lock，
        内存收益远不值得引入竞态。可在持锁内或播放任务自然结束时调用。"""
        q = self._queues.get(scope)
        if q and not q["items"] and q["current"] is None:
            self._queues.pop(scope, None)

    def _umo_of(self, scope: str) -> str:
        return self._service.umo_of(scope)

    def _start_or_none(self, scope: str, q: dict):
        """起播放任务，失败返回 None（不回滚入队——调用方决定）。

        走 service.spawn 以纳入统一生命周期管理——否则裸 create_task 在插件
        卸载/重载后仍存活，继续向已失效的 umo 发消息。spawn 在超过并发上限时
        抛 TooManyTasks，调用方必须回滚已入队的条目。
        """
        if self._stopped:
            # stop_all 已跑（或正在跑）：不再 spawn，否则卸载窗口内在途的
            # skip/add 会重新拉起引用已摘除 q dict 的僵尸播放器
            return None
        try:
            return self._service.spawn(self._player(scope))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 点歌台启动播放失败（{scope}）：{e}")
            return None

    # ──────────── 对外 ────────────
    async def add(self, scope: str, song: dict, requester: str) -> int:
        """入队；返回位置（从 1 起）。队首歌立即开始播放。

        返回码契约：-1 = 队列已满；-3 = 起不了播放（后台任务满员，或会话注册表
        查不到 umo——bot 重启后该群未再发言时无法主动发送，入队只会逐首拿
        no_umo 失败静默清空队列，故与 skip 同口径在入队前拒绝）。
        正数 = 排队位置。

        同样持 scope 锁：虽无 await 点，但不持锁的话会在 skip 的 _abort 等待窗口
        里看到 done 的旧任务并起播、写入 q["task"]，随后被 _abort 收尾的置 None
        覆盖掉登记，队列从此指向不到正在播放的任务。
        """
        async with self._lock_for(scope):
            q = self._q(scope)
            if len(q["items"]) >= MAX_QUEUE:
                # 队列非空时回收是 no-op；_q 刚建的空条目（异常不可能但防御一致）被清掉
                self._reclaim_scope(scope)
                return -1
            if not self._umo_of(scope):
                # 前置失败早退同样回收 _q 刚 setdefault 出来的空条目，
                # 与 skip/clear/_player 的回收纪律对齐（信息性：不留死条目）
                self._reclaim_scope(scope)
                return -3
            q["items"].append({"song": song, "by": requester})
            pos = len(q["items"])
            if q["task"] is None or q["task"].done():
                task = self._start_or_none(scope, q)
                if task is None:
                    # 启动失败必须回滚：否则这首歌永远滞留在队列里，
                    # 既不播放也会被 describe() 误标成「排队中」。
                    # 该 scope 的条目即此为空，一并回收（与上方两个早退分支同纪律）
                    q["items"].pop()
                    self._reclaim_scope(scope)
                    return -3
                q["task"] = task
            return pos

    def items(self, scope: str) -> list[dict]:
        # 只读查询不走 _q()：setdefault 会为任意查过的 scope 建条目，
        # 每个用过的群/私聊就永久驻留一份空条目
        q = self._queues.get(scope)
        return list(q["items"]) if q else []

    def current(self, scope: str) -> dict | None:
        q = self._queues.get(scope)
        return q["current"] if q else None

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
            if nxt is not None:
                # 会话注册表查不到 umo 与 _start_or_none 失败同属「起不了播放」：
                # 返回 None 让 handler 提示启动失败，而不是对着没播的歌播报「接下来」
                # （条目留在队首，handler 的文案已引导用户回复「切歌」重试）
                if not self._umo_of(scope):
                    return None
                # 启动失败时保持 task=None：队列仍可再 skip/clear，不至于彻底停摆
                q["task"] = self._start_or_none(scope, q)
                if q["task"] is None:
                    return None
                return nxt
            # 空队列 skip（没有播放在跑）：持锁状态下回收条目，否则本次创建的
            # scope/lock 条目无人回收，每个对空队列发过「切歌」的会话各留一份
            self._reclaim_scope(scope)
            return None

    async def clear(self, scope: str) -> int:
        async with self._lock_for(scope):
            q = self._queues.get(scope)
            if not q:
                return 0
            n = len(q["items"])
            await self._abort(q)
            q["items"].clear()
            q["current"] = None
            # 持锁状态下回收空 scope 条目（_reclaim_scope 不再动 _locks，
            # 持锁回收对等锁方完全无碍）
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
        # 先置停机闸再摘任务：窗口期在途的 skip/add 被 _start_or_none 拦下
        self._stopped = True
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
    @staticmethod
    async def _backoff(fails: int) -> None:
        """连续失败退避：三源全挂时避免 30 首队列数秒内逐首弹失败消息、打爆统计。

        单首完整耗时本身长（取流 + 下载 + 发送），退避不必大；sleep 期间被取消
        直接抛 CancelledError，由 _player 的取消路径收尾，无需额外保护。
        """
        await asyncio.sleep(min(1.0 + 0.5 * fails, 5.0))

    async def _player(self, scope: str) -> None:
        q = self._q(scope)
        me = asyncio.current_task()
        cancelled = False
        fails = 0
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
                    fails += 1
                    await self._backoff(fails)
                    continue
                try:
                    result = await self._service.send_audio(scope, song, play, note="点歌台")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 发送链路抛异常与返回失败同口径：跳过继续
                    # send_audio 内部已把「发送失败」归一成 dict 返回，能抛出来的是
                    # 链路外层意外（临时目录创建失败/消息构造异常等）——原先直接落
                    # 通用 except 会让整个队列停摆、等管理员「切歌」才恢复
                    logger.warning(f"{TAG} 点歌台发送异常，跳过：{type(e).__name__}: {e}")
                    await self._service.send_to_scope(scope, f"⚠ 点歌台发送「{song.get('name')}」失败，跳过")
                    fails += 1
                    await self._backoff(fails)
                    continue
                self._service.stats.record(
                    song.get("source", ""),
                    "play",
                    ok=bool(result.get("ok")),
                    detail=song.get("name", "")[:40],
                )
                if not result.get("ok"):
                    # 发送失败本曲没有播出内容：不加整曲等待直接接下一首，
                    # 与上方取流失败的 continue 同口径（否则连续失败时队列假死）
                    await self._service.send_to_scope(scope, f"⚠ 点歌台发送「{song.get('name')}」失败，跳过")
                    fails += 1
                    await self._backoff(fails)
                    continue
                fails = 0
                if result.get("reason") == "text_fallback":
                    await asyncio.sleep(_TEXT_FALLBACK_GAP_SEC)
                else:
                    await asyncio.sleep(wait_seconds(song, play))
        except asyncio.CancelledError:
            # 切歌/清空/卸载的取消：收尾交给 _abort / stop_all，这里不做条目回收
            cancelled = True
            raise
        except Exception as e:  # noqa: BLE001
            # 非预期异常原先只记日志：队列静默停摆，群里没人知道为什么没歌了
            logger.warning(f"{TAG} 点歌台播放循环异常（{scope}）：{e}")
            # 先归还播放者身份再发提示：提示发送是 await，期间 add() 看到 task 仍是
            # 本任务会只入队不起播放器，随后 finally 置 None 退出，条目就滞留了
            remain = len(q["items"])
            if q.get("task") is me:
                q["current"] = None
                q["task"] = None
                if not remain:
                    self._reclaim_scope(scope)
            if remain and q.get("task") is None:
                try:
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
                # 自然播完才回收空 scope 条目；取消路径上 skip/clear 正持锁等本任务
                # 退出，此时做任何收尾都可能踩到对方的收尾窗口（回收本身不持锁、
                # 且 check+pop 无 await 点，单循环内原子）
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
