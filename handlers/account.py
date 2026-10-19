"""账号路由：喜欢 / 听歌排行 / 我的歌单 / 红心操作 / 云盘 / 已购 / 最近在听。

- 喜欢：网易云（uid）与 QQ（「我喜欢」歌单）
- 听歌排行：仅网易云
- 我的歌单：网易云（uid）/ 酷狗（cookie）/ QQ（创建 + 收藏）
- 红心/取消红心：网易云与 QQ，作用于当前会话最近播放/选中的歌
"""

from __future__ import annotations

import re

from ..core import SOURCE_KG, SOURCE_NAMES, SOURCE_NCM, SOURCE_QQ
from ..core.cards import build_generic_card_data, build_playlist_card_data
from ..core.errors import ApiError
from ..core.sources import token_to_source
from .base import Route
from .explore_common import resolve_source

_SRC = r"(?:(ncm|kg|qqm|qq)\s*)?"
_RE_LIKES = rf"^\s*[#＃]?{_SRC}喜欢\s*$"
_RE_RECORD = rf"^\s*[#＃]?{_SRC}听歌排行\s*$"
_RE_MY_PLAYLIST = rf"^\s*[#＃]?{_SRC}我的歌单\s*$"
_RE_LIKE = rf"^\s*[#＃]?{_SRC}(?:红心|点赞)\s*$"
_RE_UNLIKE = rf"^\s*[#＃]?{_SRC}(?:取消红心|取消点赞)\s*$"
_RE_CLOUD = rf"^\s*[#＃]?{_SRC}云盘\s*$"
_RE_PURCHASED = rf"^\s*[#＃]?{_SRC}已购\s*$"
_RE_RECENT = rf"^\s*[#＃]?{_SRC}最近在听\s*$"
_RE_FOLLOW = rf"^\s*[#＃]?{_SRC}关注列表\s*$"
_RE_HISTORY = r"^\s*[#＃]?\s*(?:ncm|kg|qqm|qq)?\s*历史\s*$"  # 前缀可写但被有意忽略：会话历史本就跨音源

# 红心列表单次展示上限（/song/detail 按 30 个一批取详情）
_LIKES_LIMIT = 60
_DETAIL_BATCH = 30


def _pick(service, token: str, supported: tuple[str, ...]) -> str:
    """音源仲裁：显式前缀 > 配置默认 > 支持列表顺序（回落/报错统一走
    explore_common.resolve_source，与浏览类路由同一套实现防漂移；
    「显式前缀不静默换源」由 resolve_source 的 supported 校验保留）。"""
    return resolve_source(service, token, supported=supported)


def _song_items(rows: list[dict], src: str, *, sub_of=None) -> list[dict]:
    """行 → generic 卡 items 的统一构造（云盘/已购/最近在听仅字段映射差异）。"""
    items = []
    for i, r in enumerate(rows):
        sub = sub_of(r) if sub_of else (r.get("sub") or r.get("artist", ""))
        items.append(
            {
                "index": r.get("index", i + 1),
                "name": r.get("name", ""),
                "sub": sub,
                "cover": r.get("cover", ""),
                "id": r.get("sid", ""),
                "kind": "song",
                "source": src,
            }
        )
    return items


async def _ncm_uid(service) -> str:
    """网易云登录 uid；未登录抛 ApiError 提示扫码。

    函数名即平台限定：网易云是唯一用 uid 标识用户的平台（酷狗用 cookie、
    QQ 用 openid），因此以 _ncm_ 前缀命名，提醒调用方「这里只对 ncm 成立」。
    """
    status = await service.client_of(SOURCE_NCM).login_status()
    if not status.get("loggedIn") or not status.get("uid"):
        raise ApiError("网易云未登录，请先发送「ncm登录」扫码", source=SOURCE_NCM)
    return str(status["uid"])


async def _last_played_song(service, event) -> dict | None:
    """当前会话最近播放/选中的歌：优先 lastPlayed，其次当前列表第 1 首。"""
    scope = service.scope(event)
    entry = await service.sessions.get(scope)
    last = (entry.get("data") or {}).get("lastPlayed") if entry else None
    if last and last.get("sid"):
        return last
    songs = await service.sessions.songs_of(scope)
    return songs[0] if songs else None


async def run_likes(service, event):
    m = re.search(_RE_LIKES, event.message_str, re.IGNORECASE)
    src = _pick(service, m.group(1) if m else "", (SOURCE_NCM, SOURCE_QQ))
    client = service.client_of(src)
    # 平台独占能力：红心列表只有网易云与 QQ 有，且取数方式无法统一——
    # 网易云要 uid + 分批 song_detail 才能拼出曲目，QQ 直接给成品列表。
    # 这是能力差异而非解析差异，故不并入 core/catalog（详见该模块说明）。
    if src == SOURCE_NCM:
        uid = await _ncm_uid(service)
        ids = await service.call(src, "explore", client.like_list(uid))
        ids = [str(i) for i in ids if str(i).isdigit()][:_LIKES_LIMIT]
        if not ids:
            await service.reply(event, "红心列表还是空的，先在网易云里红心几首歌吧")
            return
        songs: list[dict] = []
        # 分批取详情带部分容错：某批失败时其余批已取到的照样展示，
        # 而不是整条路由报「执行失败」把好数据一起丢掉
        failed_chunks = 0
        for i in range(0, len(ids), _DETAIL_BATCH):
            chunk = ids[i : i + _DETAIL_BATCH]
            try:
                songs.extend(await service.call(src, "explore", client.song_detail(chunk)))
            except ApiError as e:
                failed_chunks += 1
                service.log_warn(f"红心详情第 {i // _DETAIL_BATCH + 1} 批失败: {e.with_source()}")
        if not songs:
            await service.reply(event, "红心歌曲详情获取失败，稍后再试")
            return
        if failed_chunks:
            await service.reply(event, f"（有 {failed_chunks} 批红心详情获取失败，以下为部分结果）")
    else:
        songs = await service.call(src, "explore", client.fav_songs(_LIKES_LIMIT))
        if not songs:
            # fav_songs 返回空是合法状态（红心为空）：与 ncm 分支同一口径，
            # 误报「获取失败」会让零红心的 QQ 用户每次都白吃一条错误提示
            await service.reply(event, f"红心列表还是空的，先在{SOURCE_NAMES.get(src, src)}里红心几首歌吧")
            return
    await service.list_to_session(
        event, f"我的红心 · {len(songs)} 首", songs, source=src, tip="回复 听N 播放"
    )


async def run_like(service, event):
    await _like_op(service, event, True)


async def run_unlike(service, event):
    await _like_op(service, event, False)


async def _like_op(service, event, like: bool):
    m = re.search(_RE_LIKE if like else _RE_UNLIKE, event.message_str, re.IGNORECASE)
    supported = (SOURCE_NCM, SOURCE_QQ)
    src = token_to_source(m.group(1) if m else "")
    explicit = src != "auto"
    # 无前缀时按最近播放的歌的音源走；取一次后面直接复用（原先取两次）
    song = None
    if src == "auto":
        song = await _last_played_song(service, event)
        src = song.get("source", "") if song else ""
    if src not in supported:
        # 用户显式写了不支持的前缀就明确说出来，不能静默换成 ncm ——
        # 否则下一行的「红心要用对应音源前缀」提示会与用户已写的前缀自相矛盾
        names = " / ".join(SOURCE_NAMES[s] for s in supported)
        if explicit:
            await service.reply(event, f"红心操作不支持{SOURCE_NAMES.get(src, src)}音源，仅支持：{names}")
            return
        src = SOURCE_NCM
    if not service.config.src_enabled(src):
        # 报真实原因（音源没配 API），别伪装成「仅支持」的能力边界
        await service.reply(event, f"{SOURCE_NAMES.get(src, src)}音源未配置 API 地址，无法红心")
        return
    client = service.client_of(src)
    # 先取歌并对齐音源，再查网易云登录态：无前缀的歌是酷狗时 src 会兜底成 ncm，
    # 登录态校验放在前面的话，ncm 未登录的用户会看到「网易云未登录」，
    # 而真正的问题是这首歌本来就不归 ncm 红心。
    # 显式前缀路径 song 还没取过；无前缀路径上面已取过一次，不再重复取
    if song is None and explicit:
        song = await _last_played_song(service, event)
    if not song or not song.get("sid"):
        await service.reply(event, "还没有可操作的歌曲，先「点歌」或「听N」一次吧")
        return
    if song.get("source") == SOURCE_KG:
        # kg 没有红心接口：若只回「要用对应音源前缀」，用户照提示切到 kg 前缀仍是
        # 死路，必须把能力边界一次说清（酷狗歌红不了，同名歌换 ncm/qq 音源才行）
        await service.reply(event, "酷狗歌曲不支持红心（仅网易云 / QQ 音乐支持）")
        return
    if song.get("source") != src:
        await service.reply(
            event,
            f"当前歌曲来自{SOURCE_NAMES.get(song.get('source'), '其他音源')}，"
            f"红心要用对应音源前缀（如「听N {song.get('source')}」切音源后再试）",
        )
        return
    if src == SOURCE_NCM:
        # 平台独占能力：网易云的红心以 uid 标识账号，必须先校验登录态；
        # QQ 用登录凭据直接操作，无需这一步。
        await _ncm_uid(service)  # 未登录直接提示
    # 平台独占能力：网易云按 sid 切换红心、QQ 按整首歌切换（like_toggle 收 song），
    # 两者入参形态不同，无法收敛成统一 resolver，故在此按平台分支。
    if src == SOURCE_NCM:
        msg = await service.call(src, "explore", client.like_song(song["sid"], like))
    else:
        msg = await service.call(src, "explore", client.like_toggle(song, like))
    await service.reply(event, f"♪ {song.get('name')} · {msg}")


async def run_record(service, event):
    m = re.search(_RE_RECORD, event.message_str, re.IGNORECASE)
    src = _pick(service, m.group(1) if m else "", (SOURCE_NCM,))
    uid = await _ncm_uid(service)
    songs = await service.call(src, "explore", service.client_of(src).user_record(uid))
    if not songs:
        await service.reply(event, "还没有听歌记录")
        return
    await service.list_to_session(event, "我的听歌排行（周榜）", songs, source=src, tip="回复 听N 播放")


async def run_my_playlists(service, event):
    m = re.search(_RE_MY_PLAYLIST, event.message_str, re.IGNORECASE)
    src = _pick(service, m.group(1) if m else "", (SOURCE_NCM, SOURCE_KG, SOURCE_QQ))
    client = service.client_of(src)
    # 平台独占能力：三家的「我的歌单」语义不同——网易云按 uid 查、酷狗走 cookie、
    # QQ 需要把「创建的歌单」与「收藏的歌单」合并去重并标注来源。
    # 合并规则属业务语义而非取数解析，故留在 handlers。
    if src == SOURCE_NCM:
        uid = await _ncm_uid(service)
        pls = await service.call(src, "explore", client.user_playlists(uid))
    elif src == SOURCE_KG:
        # 酷狗的「我的歌单」走 cookie 鉴权，无需 uid
        pls = await service.call(src, "explore", client.user_playlists())
    else:
        # QQ：创建的 + 收藏的，收藏的排后面并标注
        created = await service.call(src, "explore", client.created_songlists())
        try:
            favs = await service.call(src, "explore", client.fav_songlists(30))
        except ApiError:
            favs = []
        for p in favs:
            p["creator"] = (p.get("creator") or "收藏") + " · 收藏"
        seen = {p.get("id") for p in created}
        pls = created + [p for p in favs if p.get("id") not in seen]
    if not pls:
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 没有拿到歌单（可能需要登录）")
        return
    from ..core.formatters import format_playlist_text

    data = build_playlist_card_data(
        f"{SOURCE_NAMES[src]}我的歌单", "", pls, source=src, tip="回复 歌单 歌单名 查看曲目"
    )
    await service.reply_card_or_text(event, data, "playlist", src, format_playlist_text)


async def run_cloud(service, event):
    m = re.search(_RE_CLOUD, event.message_str, re.IGNORECASE)
    src = _pick(service, m.group(1) if m else "", (SOURCE_NCM, SOURCE_KG))
    # 平台独占能力：云盘只有网易云与酷狗有，QQ 无此概念（故 _pick 的支持列表里没有它）。
    # 两家方法名不同（user_cloud / cloud_songs）但入参与返回结构一致，
    # 属纯粹的命名差异——仍按平台分支，因为它是能力边界而非可下沉的解析逻辑。
    client = service.client_of(src)
    if src == SOURCE_NCM:
        rows = await service.call(src, "explore", client.user_cloud(30))
    else:
        rows = await service.call(src, "explore", client.cloud_songs(30))
    if not rows:
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 云盘是空的（或需要登录）")
        return
    from ..core.formatters import format_generic_text

    items = _song_items(
        rows, src, sub_of=lambda r: f"{r.get('artist', '')} · {r.get('size', '')}".strip(" ·")
    )
    data = build_generic_card_data(f"{SOURCE_NAMES[src]}云盘（{len(items)}）", "", items, source=src)
    await service.reply_card_or_text(event, data, "generic", src, format_generic_text)


async def run_purchased(service, event):
    m = re.search(_RE_PURCHASED, event.message_str, re.IGNORECASE)
    src = token_to_source(m.group(1) if m else "", default=SOURCE_KG)
    if src != SOURCE_KG:
        # 前缀指到别的音源时明确说边界（原正则只认 kg 前缀，ncm/qq 已购会静默无响应）
        await service.reply(event, "已购音乐仅酷狗提供，请用「kg已购」")
        return
    if not service.config.src_enabled(SOURCE_KG):
        await service.reply(event, "已购音乐仅支持酷狗音源（需登录）")
        return
    rows = await service.call(SOURCE_KG, "explore", service.client_of(SOURCE_KG).purchased_songs(30))
    if not rows:
        await service.reply(event, "[酷狗] 没有已购歌曲（或需要登录）")
        return
    from ..core.formatters import format_generic_text

    items = _song_items(rows, SOURCE_KG)
    data = build_generic_card_data(f"酷狗已购音乐（{len(items)}）", "", items, source=SOURCE_KG)
    await service.reply_card_or_text(event, data, "generic", SOURCE_KG, format_generic_text)


async def run_recent(service, event):
    m = re.search(_RE_RECENT, event.message_str, re.IGNORECASE)
    src = _pick(service, m.group(1) if m else "", (SOURCE_NCM, SOURCE_KG))
    client = service.client_of(src)
    # 平台独占能力：最近在听只有网易云与酷狗有（QQ 无此概念）。
    # 取数两家一致（recent_songs），唯一差异是网易云需先校验登录态（uid）。
    if src == SOURCE_NCM:
        await _ncm_uid(service)
    rows = await service.call(src, "explore", client.recent_songs(30))
    if not rows:
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 没有最近在听记录")
        return
    from ..core.formatters import format_generic_text

    items = _song_items(rows, src)
    data = build_generic_card_data(f"{SOURCE_NAMES[src]}最近在听", "", items, source=src)
    await service.reply_card_or_text(event, data, "generic", src, format_generic_text)


async def run_follow_list(service, event):
    """关注歌手列表（QQ 音源）。"""
    m = re.search(_RE_FOLLOW, event.message_str, re.IGNORECASE)
    src = _pick(service, m.group(1) if m else "", (SOURCE_QQ,))
    artists = await service.call(src, "explore", service.client_of(src).follow_singers(30))
    if not artists:
        await service.reply(event, "[QQ音乐] 关注列表是空的（或需要重新扫码登录）")
        return
    items = [
        {
            "index": a.get("index", i + 1),
            "name": a.get("name", ""),
            "sub": a.get("sub", ""),
            "cover": a.get("cover", ""),
            "id": a.get("id", ""),
            "kind": "artist",
            "source": src,
        }
        for i, a in enumerate(artists)
    ]
    from ..core.formatters import format_generic_text

    data = build_generic_card_data(
        "QQ音乐 · 关注的歌手", "", items, source=src, tip="回复 歌手 歌手名 听热门歌曲"
    )
    await service.reply_card_or_text(event, data, "generic", src, format_generic_text)


async def run_history(service, event):
    """本会话播放历史（内存，重启清空），时间从近到远。"""
    scope = service.scope(event)
    items = service.history_of(scope)
    if not items:
        await service.reply(event, "本会话还没有播放历史（成功播放过的歌会记在这里）")
        return
    songs = []
    for it in reversed(items):
        song = dict(it.get("song") or {})
        if not song.get("sid"):
            continue
        song["index"] = len(songs) + 1
        songs.append(song)
    if not songs:
        await service.reply(event, "本会话还没有播放历史")
        return
    from datetime import datetime

    ts = items[-1].get("ts", 0)
    when = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else ""
    await service.list_to_session(
        event,
        f"最近播放 · {len(songs)} 首",
        songs[:20],
        source=songs[0].get("source", "auto"),
        tip="回复 听N 重播（时间从近到远）" + (f" · 最近一首 {when}" if when else ""),
    )


def routes() -> list[Route]:
    # 账号归属类指令全部 admin 闸门：这些指令浏览/操作的是「管理员扫码登录的账号」
    # 的数据（红心、云盘、已购、最近在听、关注，含红心/取消红心两个写操作）。
    # 与「状态」「设置」同口径（auth.py/system.py 注释）：账号信息与管理操作同门槛，
    # 不能任何群成员都能浏览甚至改写管理员的账号数据、消耗其 VIP 权益
    rs = [
        Route(
            re.compile(_RE_LIKES, re.IGNORECASE),
            "mh_likes",
            "我的红心歌曲",
            run_likes,
            priority=6,
            admin=True,
        ),
        Route(
            re.compile(_RE_RECORD, re.IGNORECASE), "mh_record", "听歌排行", run_record, priority=6, admin=True
        ),
        Route(
            re.compile(_RE_MY_PLAYLIST, re.IGNORECASE),
            "mh_my_playlists",
            "我的歌单列表",
            run_my_playlists,
            priority=6,
            admin=True,
        ),
        Route(
            re.compile(_RE_LIKE, re.IGNORECASE), "mh_like", "红心当前歌曲", run_like, priority=6, admin=True
        ),
        Route(
            re.compile(_RE_UNLIKE, re.IGNORECASE),
            "mh_unlike",
            "取消红心当前歌曲",
            run_unlike,
            priority=6,
            admin=True,
        ),
        Route(
            re.compile(_RE_CLOUD, re.IGNORECASE), "mh_cloud", "我的云盘", run_cloud, priority=6, admin=True
        ),
        Route(
            re.compile(_RE_PURCHASED, re.IGNORECASE),
            "mh_purchased",
            "已购音乐",
            run_purchased,
            priority=6,
            admin=True,
        ),
        Route(
            re.compile(_RE_RECENT, re.IGNORECASE), "mh_recent", "最近在听", run_recent, priority=6, admin=True
        ),
        Route(
            re.compile(_RE_FOLLOW, re.IGNORECASE),
            "mh_follow",
            "关注歌手列表（QQ）",
            run_follow_list,
            priority=7,
            admin=True,
        ),
        Route(
            re.compile(_RE_HISTORY, re.IGNORECASE), "mh_history", "本会话播放历史", run_history, priority=6
        ),
    ]
    # 账号类指令多数直刷上游（红心列表分批取详情尤其重）：统一挂轻频控
    for r in rs:
        r.gated = True
        r.throttle = 2.0
    return rs
