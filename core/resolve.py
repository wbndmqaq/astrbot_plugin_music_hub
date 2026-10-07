"""三平台分享链接 / 卡片自动解析。

统一入口 ``handle_resolve``：按 ncm → kg → qq 顺序尝试各自的链接形态，
全部未命中且未发现任何已知分享域名时返回 False（消息不归本插件管）。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import aiohttp

from . import SOURCE_KG, SOURCE_NCM, SOURCE_QQ
from .api.http import USER_AGENT, get_session
from .errors import ApiError

# 各平台分享特征（用于判断消息是否归本插件解析）
HINTS = {
    SOURCE_NCM: re.compile(
        r"music\.163\.com|163music\.com|163cn\.tv|y\.music\.163\.com|网易云音乐|cloudmusic", re.IGNORECASE
    ),
    SOURCE_KG: re.compile(r"kugou\.com|kugou\.net|酷狗音乐|酷狗", re.IGNORECASE),
    SOURCE_QQ: re.compile(
        r"y\.qq\.com|c6\.y\.qq\.com|qq\.com/xm/qqmusic|qqmusic://|tencent\.qqmusic|QQ音乐", re.IGNORECASE
    ),
}

SHORT_LINK_RE = re.compile(r"https?://(?:163cn\.tv|c6\.y\.qq\.com|url\.cn)/\S+", re.IGNORECASE)

# 302 展开白名单（SSRF 防护）：按 hostname 精确/后缀匹配，
# 不用子串——子串会让 https://evil.com/?x=.163.com 这类地址通过校验。
# 每项必须带前导点：裸后缀 "url.cn" 会让 endswith 匹配到 evilurl.cn。
_REDIRECT_ALLOW = (".163.com", ".126.net", ".qq.com", ".gtimg.cn", ".url.cn", ".qpic.cn")
# 会发生重定向的 HTTP 状态码（307/308 与 301/302 等价，漏掉等于不校验）
_REDIRECT_STATUS = (301, 302, 303, 307, 308)

_EXPAND_TIMEOUT = aiohttp.ClientTimeout(total=8)

# 展开护栏。威胁场景：WebUI 解析接口（/api/resolve）把整段请求文本原样送进
# expand_short_links，body 上限 1MB —— 无上限时一条构造消息可命中数千条短链，
# 放大成数千次串行出站 GET（每条 8s 超时），单请求即可占死事件循环。
# 因此只在前 _EXPAND_TEXT_MAX 字符内匹配短链、每次调用最多展开 _EXPAND_MAX 条，
# 超出部分原样保留（下游已有「短链解析失败，请发完整链接」提示兜底）。
_EXPAND_MAX = 5
_EXPAND_TEXT_MAX = 2000


def is_allowed_redirect(loc: str) -> bool:
    """重定向目标是否在三平台白名单内。

    只看 hostname：query / fragment 里带白名单串不算命中，
    也不做「结尾匹配」以外的模糊判断（evil.com 子串 .163.com 必须被拒）。
    """
    if not loc:
        return False
    try:
        host = (urlsplit(loc).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == d.lstrip(".") or host.endswith(d) for d in _REDIRECT_ALLOW)


async def expand_short_links(text: str) -> str:
    """163cn.tv / c6.y.qq.com 短链 302 展开（重定向白名单校验）。

    只展开前 _EXPAND_MAX 条、只匹配前 _EXPAND_TEXT_MAX 字符（护栏理由见常量注释），
    超出的短链原样返回，不报错也不拦截。
    """

    async def _expand(url: str) -> str:
        try:
            async with get_session().get(
                url, timeout=_EXPAND_TIMEOUT, allow_redirects=False, headers={"User-Agent": USER_AGENT}
            ) as resp:
                loc = resp.headers.get("Location", "")
                if resp.status in _REDIRECT_STATUS and is_allowed_redirect(loc):
                    return loc
        except Exception:  # noqa: BLE001
            pass
        return url

    # 匹配窗口限长：1MB 文本先截前 2000 字符再找短链，窗口外的原样保留
    matches = list(SHORT_LINK_RE.finditer((text or "")[:_EXPAND_TEXT_MAX]))
    if not matches:
        return text or ""
    out = text or ""
    for m in matches[:_EXPAND_MAX]:
        expanded = await _expand(m.group(0))
        if expanded != m.group(0):
            out = out.replace(m.group(0), expanded)
    return out


def collect_text(event, *, include_json: bool = True) -> str:
    """消息文本：message_str + Plain/Json 段 + raw_message（分享卡片藏在 Json 段里）。

    include_json=False 时跳过 Json 段与 raw_message（resolveCards 关闭时只认文本链接）。
    """
    parts = []
    try:
        if event.message_str:
            parts.append(str(event.message_str))
    except Exception:  # noqa: BLE001
        pass
    if include_json:
        try:
            for comp in event.message_obj.message or []:
                kind = getattr(comp, "type", "") or comp.__class__.__name__
                if kind in ("Plain", "Json", "json") or "Json" in comp.__class__.__name__:
                    text = getattr(comp, "text", None) or getattr(comp, "data", None) or ""
                    if isinstance(text, dict):
                        text = str(text)
                    if text and text not in parts:
                        parts.append(str(text))
        except Exception:  # noqa: BLE001
            pass
        try:
            raw = event.message_obj.raw_message
            if isinstance(raw, str) and raw and raw not in parts:
                # JSON 卡片的 CQ 码里路径分隔符是转义形式（playlist\/123），不还原的话
                # 后面的路径形正则（playlist/(\d+) 等）会全部漏匹配
                parts.append(raw.replace("\\/", "/"))
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(parts)


def is_plugin_command(text: str) -> bool:
    return bool(
        re.match(r"^\s*#?\s*(mh|ncm|kg|qqm|qq|点歌|音乐|听|播放|歌词|登录|帮助)", (text or "").strip())
    )


# ──────────── 链接特征提取（handle_resolve 与 WebUI 解析工具共用） ────────────
def extract_ncm_target(text: str) -> tuple[str, str] | None:
    """网易云：返回 (kind, id)，kind ∈ playlist / album / song。"""
    for pat, kind in (
        (r"playlist\?(?:[^&\s]*&)*id=(\d+)|playlist/(\d+)", "playlist"),
        (r"album\?(?:[^&\s]*&)*id=(\d+)|album/(\d+)", "album"),
        (r"song\?(?:[^&\s]*&)*id=(\d+)|song/(\d+)", "song"),
    ):
        m = re.search(pat, text)
        if m:
            return kind, m.group(1) or m.group(2)
    return None


def extract_kg_target(text: str) -> tuple[str, str] | None:
    """酷狗：返回 (kind, value)，kind ∈ song(hash) / mixsong(id)。"""
    m = re.search(r"hash[=/]([0-9A-Fa-f]{32})", text) or re.search(r"/song/([0-9A-Fa-f]{32})", text)
    if m:
        return "song", m.group(1).upper()
    m = re.search(r"mixsong(?:id)?[=/](\d+)|kugou\.com/mixsong/(\d+)", text)
    if m:
        return "mixsong", m.group(1) or m.group(2)
    return None


def extract_qq_target(text: str) -> tuple[str, str] | None:
    """QQ 音乐：返回 (kind, value)，kind ∈ song(mid) / album(mid) / playlist(id)。"""
    m = re.search(
        r"songDetail/([0-9A-Za-z]+)|song/([0-9A-Za-z]+)|playsong\.html\?.*?songmid=([0-9A-Za-z]+)",
        text,
    )
    if m:
        return "song", next(g for g in m.groups() if g)
    m = re.search(r"album/([0-9A-Za-z]{8,})", text)
    if m:
        return "album", m.group(1)
    m = re.search(r"playlist/(\d+)|disstid=(\d+)", text)
    if m:
        return "playlist", m.group(1) or m.group(2)
    return None


def _looks_like_share(text: str) -> bool:
    """能进「冷却 + 解析」流程的最低门槛。

    share.py 的 HINTS 预过滤只认平台名，闲聊里提一句「酷狗」也会命中；冷却章与
    任何回复都必须盖在确认「这真的是一条分享」之后，否则普通聊天会被回
    「冷却中」并吞掉事件（stop_event）。判定口径：带链接 /「分享」/《歌名》
    / 可提取出具体目标 / qqmusic:// 深链。
    """
    text = text or ""
    if "qqmusic://" in text or SHORT_LINK_RE.search(text) or _SHARE_FEATURE_RE.search(text):
        return True
    return bool(extract_ncm_target(text) or extract_kg_target(text) or extract_qq_target(text))


async def handle_resolve(service, event, text: str) -> bool:
    """解析分享链接/卡片。返回是否已处理。

    与点歌共用同一冷却章：连发分享链接会连环触发取流 + 下载 + 发送，必须频控。
    未接管（返回 False）或只回了提示（如短链失败）时退还，不占用接下来的点歌冷却。
    异常兜底在 _resolve_inner 内部，这里不再包一层（原先的外层 except 是死代码）。
    """
    if not _looks_like_share(text):
        return False
    reason = service.check_cooldown(event)
    if reason:
        await service.reply(event, "操作太频繁，链接解析冷却中，稍后再试")
        return True
    handled = await _resolve_inner(service, event, text)
    if not handled:
        service.release_cooldown(event)
    return handled


async def _resolve_inner(service, event, text: str) -> bool:
    try:
        expanded = await expand_short_links(text)
        short_unexpanded = bool(SHORT_LINK_RE.search(expanded))
        text = expanded

        # ── 网易云 ──
        if HINTS[SOURCE_NCM].search(text):
            target = extract_ncm_target(text)
            if target:
                kind, tid = target
                if kind == "playlist":
                    pl, songs = await service.ncm_playlist_songs(tid)
                    if songs:
                        await service.list_to_session(
                            event, f"链接解析 · {pl.get('name') or '网易云歌单'}", songs, source=SOURCE_NCM
                        )
                        return True
                elif kind == "album":
                    detail = await service.client_of(SOURCE_NCM).album_detail(tid)
                    if detail.get("songs"):
                        await service.list_to_session(
                            event,
                            f"链接解析 · {detail['album'].get('name') or '专辑'}",
                            detail["songs"],
                            source=SOURCE_NCM,
                        )
                        return True
                else:
                    lst = await service.client_of(SOURCE_NCM).song_detail([tid])
                    if lst:
                        await service.play_song(event, lst[0], source_label="链接解析")
                        return True
            if await _keyword_fallback(service, event, text, SOURCE_NCM):
                return True

        # ── 酷狗 ──
        if HINTS[SOURCE_KG].search(text):
            target = extract_kg_target(text)
            if target:
                kind, value = target
                if kind == "song":
                    song = await service.client_of(SOURCE_KG).audio_by_hash(value)
                    if song:
                        await service.play_song(event, song, source_label="链接解析")
                        return True
                else:
                    await service.reply(event, "酷狗 mixsong 链接暂不支持直接解析，请发送带 hash 的分享链接")
                    service.release_cooldown(event)  # 只回了提示没有产出，不占冷却
                    return True
            if await _keyword_fallback(service, event, text, SOURCE_KG):
                return True

        # ── QQ 音乐 ──
        if HINTS[SOURCE_QQ].search(text) or "qqmusic://" in text:
            target = extract_qq_target(text)
            if target:
                kind, value = target
                if kind == "song":
                    song = await service.client_of(SOURCE_QQ).song_detail(value)
                    if song:
                        await service.play_song(event, song, source_label="链接解析")
                        return True
                elif kind == "album":
                    songs = await service.client_of(SOURCE_QQ).album_songs(value)
                    if songs:
                        await service.list_to_session(event, "链接解析 · QQ专辑", songs, source=SOURCE_QQ)
                        return True
                else:
                    meta, songs = await service.client_of(SOURCE_QQ).songlist_songs(value)
                    if songs:
                        await service.list_to_session(
                            event, f"链接解析 · {meta.get('name') or 'QQ歌单'}", songs, source=SOURCE_QQ
                        )
                        return True
            if "qqmusic://" in text:
                await service.reply(
                    event, "检测到 qqmusic:// 链接，请在管理面板导入凭证（聊天端已不解析 deeplink）"
                )
                service.release_cooldown(event)  # 只回了提示没有产出，不占冷却
                return True
            if await _keyword_fallback(service, event, text, SOURCE_QQ):
                return True

        if short_unexpanded and (HINTS[SOURCE_NCM].search(text) or HINTS[SOURCE_QQ].search(text)):
            await service.reply(event, "短链解析失败，请发完整链接")
            service.release_cooldown(event)  # 只回了提示没有产出，不占冷却
            return True
        return False
    except ApiError as e:
        # 音源不可用（依赖缺失）/ 需要登录等：给可操作的文案，别落进「稍后重试」
        await service.reply(event, f"链接解析失败：{e.user_msg()}")
        return False
    except Exception as e:  # noqa: BLE001 - 解析失败不影响主流程，但要让用户看见
        service.log_warn(f"链接解析异常: {e}")
        # 原先只记日志：用户只看到消息石沉大海。回一条可见的失败提示；
        # 返回 False（未产出内容）由 handle_resolve 退还冷却。
        # handler 目前只按 bool 消费返回值，无法区分「接管但失败」——
        # 若要失败也 stop_event（拦住其他插件响应分享链接），share.py 需配合扩展返回类型
        await service.reply(event, "链接解析失败，请稍后重试")
        return False


# 认定「这是一条分享/口令」的特征：带链接、「分享」字样或《歌名》引用。
# 普通聊天里偶然提到「酷狗」「网易云音乐」不该触发关键词点歌兜底。
_SHARE_FEATURE_RE = re.compile(r"分享|《[^》]+》|https?://", re.IGNORECASE)


def _keyword_from_share(text: str, source: str) -> str:
    kw = re.sub(r"https?://\S+|\[CQ:[^\]]*\]", "", text).strip()
    noise = [
        "music.163.com",
        "163music.com",
        "网易云音乐",
        "分享",
        "歌曲",
        "链接",
        "kugou.com",
        "酷狗音乐",
        "y.qq.com",
        "QQ音乐",
        "tencent.qqmusic",
        "@",
        "来自",
        "就要听",
        "- ",
    ]
    for w in noise:
        kw = kw.replace(w, " ")
    kw = re.sub(r"\s+", " ", kw).strip(" -|·")
    return kw if len(kw) >= 2 else ""


async def _keyword_fallback(service, event, text: str, source: str) -> bool:
    """分享文案关键词兜底：首个命中直接播放。

    仅对有分享特征（链接 /「分享」/《歌名》）的文本生效，闲聊提及平台名不触发。
    """
    if not _SHARE_FEATURE_RE.search(text or ""):
        return False
    kw = _keyword_from_share(text, source)
    if not kw:
        return False
    try:
        songs, _real = await service.search_songs(kw, source, limit=1)
    except Exception:  # noqa: BLE001
        return False
    if not songs:
        return False
    await service.play_song(event, songs[0], source_label="链接解析")
    return True
