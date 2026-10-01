"""点歌台路由：群内跨用户排队（排队 / 队列 / 切歌 / 清空队列）。

队列状态在 core.queue.QueueManager（内存态）；播放经会话注册表的
unified_msg_origin 主动发送，不依赖触发事件。
"""

from __future__ import annotations

import re

from .base import Route

_RE_ENQUEUE = r"^\s*#?\s*(?:排队|加入队列|点歌台)\s+(.+?)\s*$"
_RE_QUEUE = r"^\s*#?\s*(?:队列|播放队列|点歌台)\s*$"
_RE_SKIP = r"^\s*#?\s*(?:切歌|跳过|下一首)\s*$"
_RE_CLEAR = r"^\s*#?\s*清空队列\s*$"


async def run_enqueue(service, event):
    m = re.search(_RE_ENQUEUE, event.message_str, re.IGNORECASE)
    keyword = (m.group(1) if m else "").strip()
    if not keyword:
        await service.reply(event, "用法：排队 关键词")
        return
    if reason := service.check_song_request():
        await service.reply(event, f"点歌不可用：{reason}")
        return
    if service.config.cooldown_sec > 0:
        if reason := await service.check_cooldown(event):
            await service.reply(event, f"⏳ {reason}")
            return
    songs, _src = await service.search_songs(keyword, limit=3)
    if not songs:
        service.release_cooldown(event)
        await service.reply(event, f"没有搜到「{keyword}」相关的歌曲")
        return
    song = songs[0]
    scope = service.scope(event)
    requester = event.get_sender_name() or "有人"
    pos = await service.queue.add(scope, song, requester)
    if pos == -1:
        from ..core.queue import MAX_QUEUE

        await service.reply(event, f"队列已满（上限 {MAX_QUEUE} 首），稍后再试")
        return
    if pos == -2:
        await service.reply(event, "点歌台还没有绑定本会话，先在群里发一次「点歌」再排队")
        return
    src_name = {"ncm": "网易云", "kg": "酷狗", "qq": "QQ"}.get(song.get("source", ""), "")
    await service.reply(
        event,
        f"♪ 已排入队列 第 {pos} 位：{song.get('name')} - {song.get('artist')}（{requester}）[{src_name}]"
        + ("\n马上开始播放 ♪" if pos <= 1 else ""),
    )


async def run_queue(service, event):
    scope = service.scope(event)
    await service.reply(event, service.queue.describe(scope))


async def run_skip(service, event):
    scope = service.scope(event)
    nxt = service.queue.skip(scope)
    if nxt is None:
        await service.reply(event, "已切歌，队列里没有下一首了")
    else:
        song = nxt["song"]
        await service.reply(event, f"⏭ 已切歌，接下来：{song.get('name')} - {song.get('artist')}")


async def run_clear(service, event):
    scope = service.scope(event)
    n = service.queue.clear(scope)
    await service.reply(event, f"已清空队列（移除 {n} 首）" if n else "队列本来就是空的")


def routes() -> list[Route]:
    rs = [
        Route(re.compile(_RE_ENQUEUE), "mh_enqueue", "点歌台排队", run_enqueue, priority=6),
        # 队列路由要在「播放 关键词」(mh_play) 之前判定空参形式
        Route(re.compile(_RE_QUEUE), "mh_queue", "查看点歌台队列", run_queue, priority=7),
        Route(re.compile(_RE_SKIP), "mh_skip", "切歌（管理员）", run_skip, admin=True, priority=7),
        Route(
            re.compile(_RE_CLEAR), "mh_queue_clear", "清空队列（管理员）", run_clear, admin=True, priority=7
        ),
    ]
    for r in rs:
        r.gated = True
    return rs
