"""浏览发现路由：排行榜 / 新歌 / 歌手 / 专辑 / 歌单 / 热搜 / 日推 / FM / 随机。"""

from __future__ import annotations

from ..core import SOURCE_NAMES
from ..core.cards import build_comment_card_data, build_generic_card_data
from ..core.catalog import EXPAND_KINDS, resolver_for
from ..core.errors import ApiError, NotEnabledError
from ..core.sources import token_to_source


def resolve_source(service, token: str, *, prefer: str = "", supported: tuple[str, ...] = ()) -> str:
    """音源仲裁的标准实现（explore 各路由与 account._pick 共用，防止双实现漂移）：
    显式前缀 > 配置默认 > （prefer 优先的）可用音源。

    - supported 非空（账号类能力各有平台边界）：无前缀回落只在 支持列表 ∩ 可用音源
      里挑；解析结果不在支持列表时抛「该功能仅支持」——「显式前缀不静默换源」的
      语义由这道校验保留
    - 最终音源未配置时抛 NotEnabledError（ApiError 子类，调用方现有 except
      ApiError / 路由兜底都能接住）
    """
    src = token_to_source(token)
    if src == "auto":
        src = service.config.default_source
        if src == "auto":
            enabled = service.enabled_sources()
            if not enabled:
                raise ApiError("没有可用音源：请配置酷狗/网易云 API 地址", source="")
            if prefer and prefer in enabled:
                src = prefer
            else:
                pool = [s for s in supported if s in enabled] if supported else enabled
                # 聚合默认音源顺序：ncm > kg > qq
                src = "ncm" if "ncm" in pool else (pool[0] if pool else "")
    if supported and src not in supported:
        names = " / ".join(SOURCE_NAMES[s] for s in supported)
        raise ApiError(f"该功能仅支持：{names}", source=src or "")
    if not service.config.src_enabled(src):
        # 文案不带音源名：with_source 会加「[酷狗]」前缀，叠加会变成「[酷狗] 酷狗音乐音源未配置」
        raise NotEnabledError("音源未配置", source=src)
    return src


def _pick_source(service, event, token: str, *, prefer: str = "") -> str:
    """确定音源；标准实现在 resolve_source（event 仅为既有调用点签名保留，不使用）。"""
    return resolve_source(service, token, prefer=prefer)


async def _locked_source(service, event, token: str, source: str, hint: str) -> str | None:
    """限定单音源的命令：返回音源 id；不满足时回复 hint 并返回 None（调用方直接 return）。"""
    src = _pick_source(service, event, token, prefer=source)
    if src != source:
        await service.reply(event, hint)
        return None
    return src


def _to_items(rows: list[dict], kind: str, source: str = "", *, sub_key: str = "sub") -> list[dict]:
    """通用列表卡条目：从归一化行挑展示字段（run_rank / 歌手榜 / MV 榜等共用）。"""
    return [
        {
            # index 兜底 i+1，与其余列表卡构造器一致：上游行可能缺序号字段
            "index": r.get("index", i + 1),
            "name": r.get("name", ""),
            "sub": r.get(sub_key, "") or "",
            "cover": r.get("cover", ""),
            "id": r.get("id", ""),
            "tag": r.get("tag", ""),
            "kind": kind,
            "source": source,
        }
        for i, r in enumerate(rows)
    ]


async def _send_generic(
    service, event, title: str, items: list[dict], src: str, tip: str = "", subtitle: str = ""
) -> None:
    """通用列表卡的统一出口（榜单/歌手/分类/MV榜等共用）。"""
    from ..core.formatters import format_generic_text

    data = build_generic_card_data(title, subtitle, items, source=src, tip=tip)
    await service.reply_card_or_text(event, data, "generic", src, format_generic_text)


async def _send_comments(service, event, target: dict, comments: dict, src: str) -> None:
    """专辑/歌单评论卡：复用 comment 模板，构造一个类歌曲的标题对象。"""
    from ..core.formatters import format_comment_text

    fake = {
        "source": src,
        "name": target.get("name", ""),
        "artist": target.get("artist", ""),
        "cover": target.get("cover", ""),
    }
    await service.reply_card_or_text(
        event,
        build_comment_card_data(fake, comments),
        "comment",
        src,
        lambda d: format_comment_text(fake, comments),
    )


def _albums_as_items(cands: list[dict], source: str = "") -> list[dict]:
    return [
        {
            # index 兜底 i+1，与 _to_items 同纪律：上游行可能缺序号字段
            "index": c.get("index", i + 1),
            "name": c.get("name", ""),
            "sub": f"{c.get('artist', '')} · {c.get('songCount', 0)} 首",
            "cover": c.get("cover", ""),
            "id": c.get("id", ""),
            "kind": "album",
            "source": source,
        }
        for i, c in enumerate(cands)
    ]


def playlists_as_items(cands: list[dict], source: str = "") -> list[dict]:
    return [
        {
            # index 兜底 i+1，与 _to_items 同纪律：上游行可能缺序号字段
            "index": c.get("index", i + 1),
            "name": c.get("name", ""),
            "sub": f"{c.get('creator', '')} · {c.get('trackCount', 0)} 首",
            "cover": c.get("cover", ""),
            "id": c.get("id", ""),
            "kind": "playlist",
            "source": source,
        }
        for i, c in enumerate(cands)
    ]


# 展开时 meta 缺 name 时的兜底标题（与改动前逐字一致：专辑 / 歌单）
_EXPAND_DEFAULT_NAME = {"album": "专辑", "playlist": "歌单"}


async def expand_candidate(service, event, entry: dict, n: int) -> bool:
    """会话里的候选项展开：返回是否处理。

    专辑/歌单原本是 kind × source 双层分支（2×3 六段几乎相同的取数代码），
    现改为查表：kind 决定 resolver kind（EXPAND_KINDS），source 决定 resolver
    （core/catalog），本函数只负责取回元信息与曲目并下发。
    榜单 / MV / 歌手三平台方法名一致，仍直接调客户端。
    """
    kind = entry.get("kind", "")
    item_id = str(entry.get("id", "") or "")
    if not item_id:
        return False
    source = entry.get("source", "")
    resolver_kind = EXPAND_KINDS.get(kind)
    if resolver_kind is not None:
        expand = resolver_for(resolver_kind, source)
        # 查不到 resolver 说明该平台不支持此 kind：返回 False 让上层回「没有可展开的内容」
        if expand is None:
            return False
        meta, songs = await expand(service.client_of(source), item_id, entry.get("name", ""))
        if songs:
            await service.list_to_session(
                event, meta.get("name", _EXPAND_DEFAULT_NAME[kind]), songs, source=source
            )
            return True
    elif kind == "rank":
        src = source
        songs = await service.call(src, "rank", service.client_of(src).rank_songs(item_id, 30))
        if songs:
            await service.list_to_session(event, entry.get("name", "榜单"), songs, source=src)
            return True
    elif kind == "artist":
        # 歌手候选（歌手榜/热门歌手/关注列表）听N：展开成该歌手的热门歌曲列表
        src = source
        songs = await service.call(src, "explore", service.client_of(src).artist_songs(item_id, 30))
        if songs:
            await service.list_to_session(event, entry.get("name", "歌手"), songs, source=src)
            return True
    elif kind == "mv":
        fake = {"source": source, "mvid": item_id, "sid": item_id, "name": entry.get("name", "")}
        r = await service.fetch_mv_url(fake)
        url = r.get("url", "")
        if not url:
            # 上层对 False 统一回「该候选项没有可展开的内容」，取数失败会被误读成
            # 能力缺失：这里当面说清真实原因后按「已处理」返回，避免两层文案叠加
            await service.reply(event, "MV 链接获取失败，请稍后重试")
            return True
        from ..core.delivery import deliver_video

        result = await deliver_video(service, event, {"name": entry.get("name", "")}, url)
        if result.get("ok"):
            return True
        # 投递失败当面说清真实原因并按「已处理」返回：上层对 False 统一回
        # 「该候选项没有可展开的内容」，会把投递失败误读成能力缺失且双层文案叠加
        reason = str(result.get("reason") or result.get("error") or "").strip()
        await service.reply(event, "MV 发送失败，可手动观看" + (f"：{reason}" if reason else ""))
        return True
    return False


_SRC = r"(?:(ncm|kg|qqm|qq)\s*)?"
# 关键词组用可选形式 (?:\s+(.+?))?：裸「歌手」「专辑」等也要能匹配到用法提示，
# \s+(.+?) 会让空参形式整条不匹配、静默无响应（各 run 函数内已有空参用法分支）
_RE_RANK = rf"^\s*[#＃]?{_SRC}排行榜\s*(.*?)\s*$"
_RE_RANK_TOP = rf"^\s*[#＃]?{_SRC}排行推荐\s*$"
_RE_NEW = rf"^\s*[#＃]?{_SRC}新歌\s*(.*?)\s*$"
_RE_ARTIST = rf"^\s*[#＃]?{_SRC}歌手(?:\s+(.+?))?\s*$"
_RE_ARTIST_ALBUM = rf"^\s*[#＃]?{_SRC}歌手专辑(?:\s+(.+?))?\s*$"
_RE_SINGER_MV = rf"^\s*[#＃]?{_SRC}歌手MV(?:\s+(.+?))?\s*$"
_RE_SIMILAR_SINGER = rf"^\s*[#＃]?{_SRC}相似歌手(?:\s+(.+?))?\s*$"
_RE_ALBUM = rf"^\s*[#＃]?{_SRC}专辑(?:\s+(.+?))?\s*$"
_RE_PLAYLIST = rf"^\s*[#＃]?{_SRC}歌单(?:\s+(.+?))?\s*$"
_RE_HOT = rf"^\s*[#＃]?{_SRC}热搜\s*$"
_RE_RANDOM = rf"^\s*[#＃]?{_SRC}(?:来首歌|随机|放一首|来一首)\s*$"
_RE_DAILY = rf"^\s*[#＃]?{_SRC}(?:日推|每日推荐)\s*$"
_RE_FM = rf"^\s*[#＃]?{_SRC}FM\s*$"
_RE_RECOMMEND = rf"^\s*[#＃]?{_SRC}(?:歌单推荐|推荐歌单)\s*(.*?)\s*$"
_RE_NEW_ALBUM = rf"^\s*[#＃]?{_SRC}新碟\s*(.*?)\s*$"
_RE_TOP_ARTISTS = rf"^\s*[#＃]?{_SRC}歌手榜\s*$"
_RE_HOT_ARTISTS = rf"^\s*[#＃]?{_SRC}热门歌手\s*$"
_RE_MV_SEARCH = rf"^\s*[#＃]?{_SRC}(?:MV搜|mv搜|搜MV|搜mv)\s*(.*?)\s*$"
_RE_TOP_MV = rf"^\s*[#＃]?{_SRC}MV榜\s*$"
_RE_DJ = rf"^\s*[#＃]?{_SRC}电台\s*$"
_RE_BANNER = rf"^\s*[#＃]?{_SRC}banner\s*$"
_RE_THEME = rf"^\s*[#＃]?{_SRC}主题歌单\s*$"
_RE_TOP_CARD = rf"^\s*[#＃]?{_SRC}(?:好歌精选|热门精选)\s*$"
_RE_TOP_IP = rf"^\s*[#＃]?{_SRC}编辑精选\s*$"
_RE_YUEKU = rf"^\s*[#＃]?{_SRC}乐库\s*$"
_RE_GUESS = rf"^\s*[#＃]?{_SRC}猜你喜欢\s*$"
_RE_SUGGEST = rf"^\s*[#＃]?{_SRC}搜索建议\s*(.*?)\s*$"
_RE_HISTORY_DAILY = rf"^\s*[#＃]?{_SRC}历史日推\s*$"
_RE_PLAYLIST_CATS = rf"^\s*[#＃]?{_SRC}歌单分类\s*$"
_RE_HIGHQUALITY = rf"^\s*[#＃]?{_SRC}精品歌单\s*(.*?)\s*$"
_RE_RELATED_PLAYLIST = rf"^\s*[#＃]?{_SRC}相关歌单\s*(.*?)\s*$"
_RE_ALBUM_COMMENT = rf"^\s*[#＃]?{_SRC}专辑评论\s*(.*?)\s*$"
_RE_PLAYLIST_COMMENT = rf"^\s*[#＃]?{_SRC}歌单评论\s*(.*?)\s*$"
