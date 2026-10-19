"""点歌台路由：群内跨用户排队（排队 / 队列 / 切歌 / 清空队列）。

队列状态在 core.queue.RequestDesk（内存态）；播放经会话注册表的
unified_msg_origin 主动发送，不依赖触发事件。
"""

from __future__ import annotations

import re

from ..core.errors import ApiError
from .base import Route

# 关键词可选：裸「排队」也要能落到 run_enqueue 的用法提示。
# 「点歌台」别名必须带关键词：裸「点歌台」是查看队列（见 _RE_QUEUE）——若两个
# 正则都可匹配空参形式，路由优先级方向一旦变化就会落到排队用法提示而非队列展示
_RE_ENQUEUE = r"^\s*[#＃]?\s*(?:(?:排队|加入队列)(?:\s+(.+?))?|点歌台\s+(.+?))\s*$"
_RE_QUEUE = r"^\s*[#＃]?\s*(?:队列|播放队列|播放列表|点歌台)\s*$"
_RE_SKIP = r"^\s*[#＃]?\s*(?:切歌|跳过|下一首)\s*$"
_RE_CLEAR = r"^\s*[#＃]?\s*清空队列\s*$"


async def run_enqueue(service, event):
    m = re.search(_RE_ENQUEUE, event.message_str, re.IGNORECASE)
    # 「点歌台 关键词」形式的关键词在组 2，其余在组 1
    keyword = ((m.group(1) if m else "") or (m.group(2) if m else "") or "").strip()
    if not keyword:
        await service.reply(event, "用法：排队 关键词")
        return
    if reason := service.check_song_request():
        await service.reply(event, f"点歌不可用：{reason}")
        return
    # browse 轻频控放在盖章（check_cooldown）之前：被节流拒绝时还没有盖冷却章，
    # 无产出却占满整个冷却窗口违反「无产出即退章」纪律（原先先盖章再拒绝，
    # 2 秒内发过浏览类指令的「排队 x」会白占整个冷却间隔）
    if reason := service.check_browse(event, 2.0):
        await service.reply(event, f"⏳ {reason}")
        return
    # check_cooldown 内部已判 cooldown_sec <= 0 直接放行，无需外层再守一道
    if reason := service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    try:
        songs, _src = await service.search_songs(keyword, limit=3)
    except ApiError as e:
        # 对齐 play.py _song_request 的退章纪律：搜索没播出任何内容就退冷却，
        # 不让一个报错的关键词（酷狗未登录 152 是常态错误）占住整群点歌间隔
        service.release_cooldown(event)
        await service.reply(event, e.with_source())
        return
    if not songs:
        service.release_cooldown(event)
        await service.reply(event, f"没有搜到「{keyword}」相关的歌曲")
        return
    song = songs[0]
    scope = service.scope(event)
    requester = event.get_sender_name() or "有人"
    pos = await service.queue.add(scope, song, requester)
    if pos == -1:
        service.release_cooldown(event)
        from ..core.queue import MAX_QUEUE

        await service.reply(event, f"队列已满（上限 {MAX_QUEUE} 首），稍后再试")
        return
    if pos == -3:
        # 后台任务已达并发上限，入队已回滚；不提示具体上限，避免误导
        service.release_cooldown(event)
        await service.reply(event, "系统繁忙，排队没有成功，稍后再试")
        return
    src_name = {"ncm": "网易云", "kg": "酷狗", "qq": "QQ"}.get(song.get("source", ""), "")
    # 入队成功（pos>1）不退冷却章是有意行为：点歌台以占用冷却防连点刷队列，
    # 与点歌主流程「只出列表未播歌就退章」的语义不同——排队已真实写入队列状态，
    # 成功即算产出，章留着挡住紧跟着的连发「排队 x」
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
    nxt = await service.queue.skip(scope)
    if nxt is None:
        # skip 对「没有下一首」与「下一首启动失败」都返回 None，用队列余量区分文案
        if service.queue.items(scope):
            await service.reply(event, "已切歌，但下一首播放任务启动失败，回复「切歌」重试")
        else:
            await service.reply(event, "当前没有正在播放的队列，先「点歌」或「排队」吧")
    else:
        song = nxt["song"]
        await service.reply(event, f"⏭ 已切歌，接下来：{song.get('name')} - {song.get('artist')}")


async def run_clear(service, event):
    scope = service.scope(event)
    n = await service.queue.clear(scope)
    await service.reply(event, f"已清空队列（移除 {n} 首）" if n else "队列本来就是空的")


def routes() -> list[Route]:
    rs = [
        Route(re.compile(_RE_ENQUEUE, re.IGNORECASE), "mh_enqueue", "点歌台排队", run_enqueue, priority=6),
        # 队列路由要在「播放 关键词」(mh_play) 之前判定空参形式
        Route(re.compile(_RE_QUEUE, re.IGNORECASE), "mh_queue", "查看点歌台队列", run_queue, priority=7),
        Route(
            re.compile(_RE_SKIP, re.IGNORECASE), "mh_skip", "切歌（管理员）", run_skip, admin=True, priority=7
        ),
        Route(
            re.compile(_RE_CLEAR, re.IGNORECASE),
            "mh_queue_clear",
            "清空队列（管理员）",
            run_clear,
            admin=True,
            priority=7,
        ),
    ]
    for r in rs:
        r.gated = True
    return rs
