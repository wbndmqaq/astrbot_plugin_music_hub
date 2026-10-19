"""QQ音乐音源客户端（基于 qqmusic-api-python，进程内直连官方接口）。

- 单例 ``Client``：设备指纹落盘（data/plugin_data/<plugin>/qq_device.json），
  避免每次重启都是新设备触发风控
- 登录态：``Credential`` JSON 存配置 ``qq.cookie``（扫码登录自动写入）
- 取流：CDN dispatch + get_song_urls 按音质阶梯尝试；匿名 VIP 歌 104003，
  免费歌可用；试听档 SpecialSongFileType.TRY 匿名可用
"""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from astrbot.api import logger

from ..errors import ApiError, NotEnabledError
from ..quality import QQ_LABEL, QQ_LADDER, ladder_for
from .http import harden_sensitive_file, list_of
from .registry import register

# qqmusic-api 是可选重依赖：缺失时给出清晰指引而不是 import 崩溃
try:
    from qqmusic_api import Client, Credential
    from qqmusic_api.core.exceptions import (
        ApiDataError,
        BaseApiException,
        CredentialExpiredError,
        CredentialInvalidError,
        HTTPError,
        NetworkError,
        RatelimitedError,
        TimeoutNetworkError,
    )
    from qqmusic_api.models.login import QRCodeLoginEvents, QRLoginType
    from qqmusic_api.modules.login_utils import QRCodeLoginSession
    from qqmusic_api.modules.search import SearchType
    from qqmusic_api.modules.song import SongFileInfo, SongFileType, SpecialSongFileType
except ImportError as _e:  # pragma: no cover
    Client = None
    Credential = None
    _IMPORT_ERROR = _e

    # except 子句要求可捕获类型：库缺失时方法入口先抛 NotEnabledError，占位异常
    # 永远不会真正命中，只保证 `except BaseApiException` 语句本身合法
    class BaseApiException(Exception):
        pass

    CredentialExpiredError = NetworkError = RatelimitedError = ()
    TimeoutNetworkError = ()
    ApiDataError = CredentialInvalidError = HTTPError = ()
    SearchType = None
    SongFileInfo = None
    SongFileType = None
    SpecialSongFileType = None
    QRCodeLoginEvents = None
    QRLoginType = None
    QRCodeLoginSession = None

_NO_LOGIN_CODES = {104003}  # 104004=VKey 获取失败（换源重试可解）、104013=设备受限，都不该归因于"未登录"

# core.search 传的是平台无关的参数名，QQ 侧映射到 SearchType 的整数值。
# 放在模块级而非方法内：库未安装时 SearchType 为 None，映射表仍可安全构建。
_QQ_TYPE_BY_NAME: dict[str, int] = {}
if SearchType is not None:
    for _name in ("SONGLIST", "ALBUM", "SINGER", "MV", "SONG"):
        _member = getattr(SearchType, _name, None)
        if _member is not None:
            _QQ_TYPE_BY_NAME[_name.lower()] = int(_member)


def available() -> bool:
    return Client is not None


def import_error() -> str:
    return str(_IMPORT_ERROR) if not available() else ""


async def _limit() -> None:
    """单次上游请求占一个限速槽位。限速器故障不能阻断主链路。

    与 _guard_sdk 的入口 acquire 分工：_guard_sdk 管「一个公开方法 == 一次
    SDK 请求」的方法；取流阶梯内的 _url_for/_cdn 单独调用本函数按真实请求
    计数（阶梯一档一请求，song_url_best 自身不再 acquire，避免双扣）。"""

    from ..ratelimit import limiter

    try:
        await limiter.acquire("qq")
    except Exception:  # noqa: BLE001 - 限速器故障不能阻断主链路
        pass


def _guard_sdk(fn):
    """SDK 调用统一出口：BaseApiException → _map，ValidationError → 固定文案 ApiError。

    库对上游报文的 model_validate 无任何包装，pydantic.ValidationError（ValueError
    子类）不是 BaseApiException，会穿透所有 `except BaseApiException` 调用点，把
    "1 validation error for ..." 原文甩给用户；在出口统一兜住，方法体不必逐个加 try。

    同时是 QQ 限速的统一入口：ncm/kg 走 request_json（内含 limiter.acquire），
    QQ 直连 SDK 从不过限速器——而 QQ 恰是最易触发风控的源（匿名 + 取流爬阶梯
    每首歌最多连打 6+2 次）。在此按源最小间隔节流，对齐 ratelimit 模块声明的语义。

    只挂公开方法（方法体恰好一次上游请求）：内部 accessor get_client 绝不能挂，
    否则每个公开方法经它再 acquire 一次，限速预算被静默翻倍。
    """

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        from ..ratelimit import limiter

        try:
            await limiter.acquire("qq")
        except Exception:  # noqa: BLE001 - 限速器故障不能阻断主链路
            pass
        try:
            return await fn(self, *args, **kwargs)
        except BaseApiException as e:
            raise self._map(e) from e
        except ValueError as e:
            raise ApiError("QQ 音乐响应解析失败，请稍后重试", source="qq") from e

    return wrapper


def _strip_em(text: str) -> str:
    """剥掉搜索高亮 <em>/</em> 标签。

    QQ 搜索命中关键词时 name/singer 等文本字段带 <em> 高亮（实测 playlist/album
    类型高频出现），卡片与 WebUI 都做转义不解析，原样透出会显示字面 <em> 文本。
    只剥 em：剥全部标签会破坏名字里合法的「<」字面量。"""
    return re.sub(r"</?em>", "", text).strip()


def _norm_song(item, idx: int = 0) -> dict | None:
    """Song/SongSearch 模型 → 统一歌曲结构。"""
    if item is None:
        return None
    name = _strip_em(getattr(item, "name", "") or getattr(item, "title", "") or "")
    if not name:
        return None
    singers = getattr(item, "singer", None) or []
    artist = " / ".join(_strip_em(s.name) for s in singers if getattr(s, "name", ""))
    album = getattr(item, "album", None)
    album_name = _strip_em(getattr(album, "name", "")) if album else ""
    album_mid = getattr(album, "mid", "") if album else ""
    cover = f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{album_mid}.jpg" if album_mid else ""
    interval = int(getattr(item, "interval", 0) or 0)
    pay = getattr(item, "pay", None)
    pay_play = int(getattr(pay, "pay_play", 0) or 0) if pay else 0
    mv = getattr(item, "mv", None)
    vid = getattr(mv, "vid", "") if mv else ""
    return {
        "index": idx + 1,
        "source": "qq",
        "sid": getattr(item, "mid", "") or "",
        "sid2": str(getattr(item, "id", 0) or ""),
        "name": name,
        "artist": artist,
        "album": album_name or "",
        "albumMid": album_mid,
        "cover": cover,
        "duration": f"{interval // 60:02d}:{interval % 60:02d}" if interval > 0 else "",
        "dtMs": interval * 1000,
        "pay": pay_play in (1, 4),
        "trial": False,
        "mvid": vid or "",
        "raw": {},
    }


def _norm_songlist(item, idx: int = 0) -> dict | None:
    title = _strip_em(getattr(item, "title", "") or getattr(item, "name", "") or "")
    if not title:
        return None
    # 创建者昵称字段三种模型不同名：搜索 SongListSearch.nickname /
    # 推荐 RecommendSonglistItem.creator_nick / 自建 UserPlaylistSummary.nick
    creator = _strip_em(
        getattr(item, "nickname", "") or getattr(item, "creator_nick", "") or getattr(item, "nick", "") or ""
    )
    return {
        "index": idx + 1,
        "source": "qq",
        "id": str(getattr(item, "dirid", 0) or getattr(item, "id", 0) or ""),
        "name": title,
        "cover": getattr(item, "picurl", "") or "",
        "trackCount": int(getattr(item, "songnum", 0) or 0),
        "playCount": int(getattr(item, "listennum", 0) or 0),
        "creator": creator,
    }


def _norm_album(item, idx: int = 0) -> dict | None:
    name = _strip_em(getattr(item, "name", "") or getattr(item, "title", "") or "")
    if not name:
        return None
    mid = getattr(item, "mid", "") or ""
    # 歌手字段四种形态：AlbumSearch（搜索）覆写 singer 为含 <em> 高亮的字符串、
    # 结构化列表在 singer_list；基类 Album / 详情是 .singer 列表；新专辑是 .singers；
    # 歌手专辑 AlbumBrief 是 .singer_name 字符串
    raw_singer = getattr(item, "singer", None)
    structured = getattr(item, "singer_list", None) or getattr(item, "singers", None)
    if not isinstance(raw_singer, str):
        structured = structured or raw_singer or []
    if structured:
        artist = " / ".join(_strip_em(s.name) for s in structured if getattr(s, "name", ""))
    elif isinstance(raw_singer, str):
        artist = _strip_em(raw_singer)
    else:
        artist = str(getattr(item, "singer_name", "") or "")
    return {
        "index": idx + 1,
        "source": "qq",
        "id": mid or str(getattr(item, "id", "") or ""),
        "name": name,
        "cover": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{mid}.jpg"
        if mid
        else (getattr(item, "pic", "") or ""),
        "artist": artist,
        "songCount": int(
            getattr(item, "songnum", 0)
            or getattr(item, "total_num", 0)
            or getattr(item, "song_count", 0)
            or 0
        ),
    }


def _norm_singer(item, idx: int = 0) -> dict | None:
    name = _strip_em(getattr(item, "name", "") or "")
    if not name:
        return None
    mid = getattr(item, "mid", "") or ""
    return {
        "index": idx + 1,
        "source": "qq",
        "id": mid or str(getattr(item, "id", "") or ""),
        "name": name,
        "cover": f"https://y.gtimg.cn/music/photo_new/T001R300x300M000{mid}.jpg" if mid else "",
        "sub": "",
    }


def _fmt_pub_time(value) -> str:
    """pub_time 是秒级时间戳 int（个别场景为字符串），统一格式化成日期文本。"""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d", time.localtime(value)) if value > 0 else ""
    s = str(value).strip()
    if s.isdigit():
        return time.strftime("%Y-%m-%d", time.localtime(int(s)))
    return s


def _safe_int(v) -> int:
    """整型转换带 isdecimal 前置：属性值漂移成非数字串时裸 int() 抛的 ValueError
    会被 _guard_sdk 误归因为「响应解析失败」（对齐 rank_songs/comments 的校验口径）。"""
    if isinstance(v, int):
        return v
    s = str(v or "").strip()
    return int(s) if s.isdecimal() else 0


# 逐字歌词的三层时间标记：[ti:..] 元信息 / [start,dur] 行级 / (start,dur) 字级
_QRC_META_RE = re.compile(r"^\[[a-zA-Z]+:[^\]]*\]\s*")
_QRC_LINE_TS_RE = re.compile(r"^\[\d+,\d+\]\s*")
_QRC_WORD_TS_RE = re.compile(r"\(\d+,\d+\)")
_QRC_CONTENT_RE = re.compile(r'LyricContent="([^"]*)"', re.S)


def parse_qrc(qrc_xml: str) -> list[str]:
    """QRC（逐字歌词）→ 行文本列表。失败返回空列表。

    上游有两种形态，qqmusic-api 不同版本返回的结构不同，两种都要认：
    - ``<Line_ txt="...">`` 逐行元素（旧格式）
    - 整段歌词塞在 ``<Lyric_1 LyricContent="...">`` 属性里，行形如
      ``[0,2250]晴(0,160)天(160,160)``（0.8.x 实测格式，且不含 <Line_ 元素）
    """
    if not qrc_xml or "QrcInfos" not in qrc_xml:
        return []
    # 属性值内的 XML/HTML 实体（&quot; &amp; &#39;…）在**捕获后**反转义：
    # 先对整段反转义会把值内的 &quot; 变成真实引号、破坏 txt="([^"]*)" 的边界匹配
    import html as _html

    lines = []
    for m in re.finditer(r'<Line_[^>]*txt="([^"]*)"[^>]*>', qrc_xml):
        txt = _html.unescape(m.group(1)).strip()
        if txt:
            lines.append(txt)
    if not lines:
        # 行内容量词带上界：无闭合 </Line_ 的病态输入（上游数据漂移）下，
        # 无上界的 .*? 会从每个 <Line_ 起点扫到串尾、整段 O(n²)。真实逐字行
        # 远小于 600 字符，截断最多丢一条残行文本
        for m in re.finditer(r"<Line_[^>]*>(.{0,600}?)</Line_", qrc_xml, re.S):
            txt = re.sub(r"<[^>]+>", "", m.group(1))
            txt = _QRC_WORD_TS_RE.sub("", txt).strip()
            if txt:
                lines.append(txt)
    if not lines:
        m = _QRC_CONTENT_RE.search(qrc_xml)
        if m:
            for raw in _html.unescape(m.group(1)).splitlines():
                line = _QRC_META_RE.sub("", raw.strip())
                line = _QRC_LINE_TS_RE.sub("", line)
                line = _QRC_WORD_TS_RE.sub("", line).strip()
                if line:
                    lines.append(line)
    return lines


def parse_lrc(lrc: str) -> list[str]:
    """LRC 文本 → 行文本（去时间戳/元信息）。"""
    out = []
    for line in (lrc or "").splitlines():
        line = line.strip()
        if (
            not line
            or line.startswith("[ti:")
            or line.startswith("[ar:")
            or line.startswith("[al:")
            or line.startswith("[by:")
            or line.startswith("[offset:")
        ):
            continue
        txt = re.sub(r"^\[\d{1,3}:\d{1,3}(?:[.:]\d{1,3})?\]", "", line).strip()
        # 第二遍同时清掉行中/行尾的时间戳与逐字增强标记（<mm:ss.xx>），只留纯文本
        txt = re.sub(r"[\[<]\d{1,3}:\d{1,3}(?:[.:]\d{1,3})?[\]>]", "", txt).strip()
        if txt:
            out.append(txt)
    return out


class QQClient:
    source = "qq"

    def __init__(self, config, device_path: Path):
        self._config = config
        self._device_path = device_path
        self._client: Client | None = None
        self._lock = asyncio.Lock()
        self._cdn_cache: tuple[float, str] = (0.0, "")
        # 凭证缓存：(载入时刻, credential)。取流是热路径，每次 json.loads 配置串太浪费
        self._cred_cache: tuple[float, object] = (0.0, None)
        # https 失败回退记忆：mid → 上次交付的完整 https 链接。只服务紧随其后的
        # 下载重试（对同一首歌会再取一次流），条目数以并发下载的几首歌为界
        self._https_tried: dict[str, str] = {}

    def ready(self) -> bool:
        """重依赖是否就绪。与 ncm/kg 保持同为方法（不是 property），
        否则契约自检的 callable 判定会把它误判为「未实现」。"""
        return available()

    def _harden_device_file(self) -> None:
        """设备指纹是风控敏感数据，尽力收紧文件访问面（对齐 webui_secret.key 的做法）。

        与密钥文件不同：收紧失败只记日志不删文件——指纹丢了只是重新生成一次风控画像，
        不值得为它打断登录链路；库在指纹变化时会重写文件，这里按 best-effort 处理。
        收紧逻辑与酷狗设备 cookie 共用 http.harden_sensitive_file。"""
        harden_sensitive_file(self._device_path)

    _CRED_TTL = 5.0

    def _credential(self):
        if not available():
            return None
        ts, cached = self._cred_cache
        if cached is not None and time.monotonic() - ts < self._CRED_TTL:
            return cached
        raw = self._config.src_cookie("qq")  # qq.cookie 存 Credential JSON
        cred = None
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("musickey"):
                    cred = Credential.model_validate(data)
            except (ValueError, TypeError):
                cred = None
        # 只在解析出有效凭证时更新缓存：解析失败保留旧值，
        # 否则一次配置写入失败就会把有效登录态冲成 None
        if cred is not None:
            self._cred_cache = (time.monotonic(), cred)
            return cred
        if not raw:
            # 配置里已无 cookie（WebUI 改配置登出 / clear_src_cookie）：这是明确的清除
            # 信号，必须连缓存一起清掉，否则旧凭证无限期复用，客户端永远带着已失效
            # 的 key 打上游（invalidate 只挂在 logout/refresh/扫码三条路径上，覆盖不到它）
            self.invalidate_credential()
            # 本次就按匿名走：末行回退 cached 会把旧凭证再用一次（清 cookie 后首个
            # 取流仍按登录态判断 pay 跳档、login_status 多显示一次已登录）
            return None
        # raw 存在但解析失败（写入中途读到半截 JSON 等瞬态）：保留旧值不动
        return cached

    def invalidate_credential(self) -> None:
        """登录态变更后主动失效缓存（扫码成功 / 登出 / 刷新失败）。"""
        self._cred_cache = (0.0, None)

    async def get_client(self):
        """懒加载单例 Client（带登录态）。未安装库抛 NotEnabledError。

        不挂 _guard_sdk：这是内部 accessor 不发上游请求，挂了会让每个公开方法
        双扣限速槽位（外层方法 acquire 一次 + 这里再 acquire 一次）。"""
        if not available():
            raise NotEnabledError(
                f"qqmusic-api-python 未安装：pip install qqmusic-api-python（{import_error()}）", source="qq"
            )
        async with self._lock:
            if self._client is None:
                # mkdir 是同步 syscall：与 icacls 同等对待丢线程池（网络盘/杀毒扫描可卡住事件循环）
                try:
                    await asyncio.to_thread(self._device_path.parent.mkdir, parents=True, exist_ok=True)
                except OSError:
                    pass
                client = Client(device_path=str(self._device_path))
                # icacls 子进程同步且最坏 5s：必须丢线程池，否则冷启动卡住整个事件循环
                await asyncio.to_thread(self._harden_device_file)
                try:
                    await client.__aenter__()
                except BaseException:
                    # 半初始化的 client 不能留在 self._client：非 None 会跳过后面的
                    # 创建与 __aenter__，所有调用永远复用这个没就绪的实例直到重启
                    self._client = None
                    raise
                self._client = client
            # 仅在 client 尚无有效登录态时补一次注入。库不会替插件维护 client 上的
            # 凭证：只有 refresh 不传参 / phone_authorize / logout 三个路径会回写，
            # 插件的 refresh 恒传显式 cred、QQ/微信/MOBILE 扫码路径（qr_consume）
            # 库也不回写——那两处成功后由插件显式回写（见对应方法）。
            # 这里同样不能每次调用都重写：会把刚拿到/刚刷新的有效凭证冲掉。
            # 判据必须是 musicid/musickey，不能是 `credential is None`——
            # qqmusic_api 的 setter 是 `value or Credential()`，None 会被兜底成空凭证，
            # 判 None 永远为假，配置里的凭证在插件重启后永远注入不进来。
            # 配置里本就无 cookie（匿名）时直接跳过：否则 _credential() 恒 None →
            # setter 兜底空凭证 → 判据恒真，每次调用都白付一次锁 + 线程跳。
            cred = getattr(self._client, "credential", None)
            if not (cred and cred.musicid and cred.musickey) and self._config.src_cookie("qq"):
                self._client.credential = await asyncio.to_thread(self._credential)
            return self._client

    async def close(self) -> None:
        # 与 get_client 同锁：并发窗口里 get_client 可能把正在关闭的 client 当活实例
        async with self._lock:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
            # close 后旧凭证不该再注入新 client（对齐 logout 的口径）
            self.invalidate_credential()

    # ──────────── 搜索 ────────────
    def search_type(self, param: str):
        """把 core 层的数据驱动参数名（"songlist"/"album"/"singer"）转成 SearchType。

        平台适配点：core.search 不必知道 QQ 用的是枚举而不是裸值。
        """
        st = _QQ_TYPE_BY_NAME.get(param)
        # 不能写 `if st`：SONG=0 会被判假返回 None，core/search 把 None 当
        # 「该类型不支持」静默返回空列表
        return SearchType(st) if st is not None else None

    @_guard_sdk
    async def search(
        self, keyword: str, limit: int = 10, type_: SearchType | None = None, page: int = 1
    ) -> list[dict]:
        """page 从 1 起（ncm 是 0 起）——三源 page 语义不统一，调用方传值时注意。"""
        client = await self.get_client()
        st = type_ or SearchType.SONG
        if isinstance(st, str):  # 容忍直接传参数名
            st = self.search_type(st)
            if st is None:
                # 未知名返回空列表（对齐 ncm/kg 的「该类型不支持」口径），
                # 静默回退歌曲搜索会让调用方拿到类型不符的结果
                return []
        res = await client.search.search_by_type(keyword=keyword, search_type=st, num=limit, page=page)
        if st == SearchType.SONG:
            return [s for s in (_norm_song(x, i) for i, x in enumerate(res.song or [])) if s][:limit]
        if st == SearchType.SONGLIST:
            return [s for s in (_norm_songlist(x, i) for i, x in enumerate(res.songlist or [])) if s][:limit]
        if st == SearchType.ALBUM:
            return [s for s in (_norm_album(x, i) for i, x in enumerate(res.album or [])) if s][:limit]
        if st == SearchType.SINGER:
            return [s for s in (_norm_singer(x, i) for i, x in enumerate(res.singer or [])) if s][:limit]
        if st == SearchType.MV:
            mvs = [self._norm_mv(x, i) for i, x in enumerate(res.mv or [])]
            return [m for m in mvs if m][:limit]
        return []

    @staticmethod
    def _norm_mv(item, idx: int = 0, fallback_artist: str = "") -> dict | None:
        name = (getattr(item, "name", "") or getattr(item, "title", "") or "").strip()
        if not name:
            return None
        vid = getattr(item, "vid", "") or ""
        singers = getattr(item, "singer", None) or []
        if not singers:
            sname = getattr(item, "singer_name", "")
            # fallback_artist：singer.get_mv_list 的条目类型 VideoBrief 没有任何
            # singer 字段，artist_mvs 把已查得的歌手名透传进来兜底
            artist = sname or fallback_artist
        else:
            artist = " / ".join(s.name for s in singers if getattr(s, "name", ""))
        # VideoBrief 的封面/播放量字段是 picurl / playcnt
        pic = getattr(item, "pic", "") or getattr(item, "picurl", "") or ""
        if not pic:
            pic = f"https://y.gtimg.cn/music/photo_new/T015R300x202M000{vid}.jpg" if vid else ""
        dur = int(getattr(item, "duration", 0) or 0)
        return {
            "index": idx + 1,
            "id": vid,
            "name": name,
            "artist": artist,
            "cover": pic,
            "duration": f"{dur // 60:02d}:{dur % 60:02d}" if dur > 0 else "",
            "playCount": int(getattr(item, "play_count", 0) or getattr(item, "playcnt", 0) or 0),
        }

    @_guard_sdk
    async def song_detail(self, sid: str) -> dict | None:
        client = await self.get_client()
        res = await client.song.get_detail(sid)
        track = getattr(res, "track", None)
        return _norm_song(track)

    @_guard_sdk
    async def similar_songs(self, song_id: str, limit: int = 10) -> list[dict]:
        # 上游按数字 songid 查相似；actions 层会传 sid2 或回退 sid(mid)，mid 非数字时
        # 裸 int() 抛的 ValueError 不在任何 except 面里，这里前置归一成明确报错
        song_id = int(song_id) if str(song_id).isdecimal() else 0
        if not song_id:
            raise ApiError("歌曲缺少有效 id", source="qq")
        client = await self.get_client()
        res = await client.song.get_similar_song(song_id)
        # 返回 .song 是 SimilarSongGroup 分组列表，歌曲在各组的 .song 里
        groups = getattr(res, "song", None) or []
        songs: list = []
        for g in groups:
            songs.extend(getattr(g, "song", None) or [])
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s][:limit]

    @_guard_sdk
    async def other_versions(self, song: dict, limit: int = 15) -> list[dict]:
        """同曲其他版本（Live/伴奏/重录…）。参数两可：song id 或 mid。"""
        # sid2 非数字（如误传 mid）时不能裸 int()：ValueError 会被 _guard_sdk
        # 误归因为「响应解析失败」；按两可语义回退 sid(mid)
        raw_sid2 = str(song.get("sid2") or "")
        value = int(raw_sid2) if raw_sid2.isdecimal() else 0
        value = value or song.get("sid") or ""
        if not value:
            return []
        client = await self.get_client()
        res = await client.song.get_other_version(value)
        songs = getattr(res, "data", None) or []
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s][:limit]

    @_guard_sdk
    async def suggest(self, keyword: str) -> list[dict]:
        """搜索联想（quick_search HTTP 接口）。"""
        client = await self.get_client()
        res = await client.search.quick_search(keyword)
        out = []
        for key in ("song", "singer", "album"):
            cat = getattr(res, key, None)
            for it in getattr(cat, "itemlist", None) or []:
                name = _strip_em(getattr(it, "name", "") or "")
                if not name:
                    continue
                out.append({"name": name, "sub": _strip_em(getattr(it, "singer", "") or ""), "kind": key})
        return out[:10]

    @_guard_sdk
    async def hot_search(self) -> list[dict]:
        client = await self.get_client()
        res = await client.search.get_hotkey()
        return [
            {"index": i + 1, "word": it.query or "", "hot": it.score or ""}
            for i, it in enumerate(getattr(res, "vec_hotkey", None) or [])
            if it.query
        ][:15]

    # ──────────── 取流 ────────────
    _CDN_TTL = 300.0

    async def _cdn(self) -> str:
        """分发地址 5 分钟缓存：每首歌取流都要先查一次 dispatch，连播/换档时省得反复打。

        查询失败不缓存（立即回退常量地址），下次取流再试。
        """
        ts, base = self._cdn_cache
        if base and time.monotonic() - ts < self._CDN_TTL:
            return base
        client = await self.get_client()
        try:
            await _limit()  # dispatch 是真实上游请求：按请求计数
            res = await client.song.get_cdn_dispatch()
            sip = getattr(res, "sip", None) or []
            if sip:
                base = str(sip[0])
                # 下游按 cdn + purl 裸拼：sip 项无尾斜杠会拼出坏 URL
                if not base.endswith("/"):
                    base += "/"
                self._cdn_cache = (time.monotonic(), base)
                return base
        except Exception:  # noqa: BLE001 - 纯尽力而为的查询：库的 pydantic ValidationError
            # （ValueError 子类）不是 BaseApiException，会从这里穿出去炸掉 song_url_best
            # 里已经成功取得的 URL；查询失败本就该回退常量地址，任何异常一视同仁
            pass
        return "https://isure.stream.qqmusic.qq.com/"

    def _warn_stale_ladder(self, type_map: dict) -> None:
        """启动后首次取流前自检：SDK 改名枚举成员时阶梯全档 getattr 落空，
        最后只剩一句笼统的「无法获取播放链接」——提前 warning 指路（对齐
        base.py missing_methods「启动期暴露问题」的设计）。"""
        if getattr(self, "_ladder_checked", False):
            return
        self._ladder_checked = True
        if SongFileType is None:
            return
        missing = [
            name for names in type_map.values() for name in names if name and not hasattr(SongFileType, name)
        ]
        # 试听兜底档在 SpecialSongFileType 上：SDK 改名 TRY 时试听分支会裸 AttributeError
        if SpecialSongFileType is not None and not hasattr(SpecialSongFileType, "TRY"):
            missing.append("SpecialSongFileType.TRY")
        if missing:
            logger.warning(
                f"[music_hub] qqmusic-api 版本不匹配：音质档位 {sorted(set(missing))} "
                "在 SongFileType 中不存在，对应档位将被跳过；请升级或降级 qqmusic-api-python"
            )

    async def _url_for(self, mid: str, ftype) -> dict:
        await _limit()  # 阶梯一档一请求：按请求计数（song_url_best 自身不 acquire）
        client = await self.get_client()
        res = await client.song.get_song_urls([SongFileInfo(mid=mid, file_type=ftype)])
        data = getattr(res, "data", None) or []
        info = data[0] if data else None
        if info is None:
            return {"ok": False, "code": None, "url": ""}
        # result 是必填 int，成功值为 0 —— 不能用 `or` 兜底（0 会被短路成 -1）
        raw = getattr(info, "result", None)
        result = -1 if raw is None else int(raw)
        purl = getattr(info, "purl", "") or ""
        return {"ok": result == 0 and bool(purl), "code": result, "url": purl}

    _HTTPS_FALLBACK_MAX = 8

    def _stream_url(self, mid: str, purl: str, cdn: str) -> str:
        """purl → 完整媒体链接。CDN dispatch 的 sip 项可能回 http://：统一升 https
        （y.gtimg/isure 均支持，部分平台对 http 媒体下发不友好）。

        个别 sip 域可能不支持 TLS：https 失败后下载重试会带着同一首歌再次进来取流，
        此时交付原始 http 形式兜底——重试链路对「同 URL」会直接放弃，不换形式等于
        没有回退。http 兜底只交付一次，再失败回到 https 起步。"""
        raw = purl if purl.startswith("http") else cdn + purl
        url = raw.replace("http://", "https://", 1)
        if url == raw:
            return url
        if self._https_tried.get(mid) == url:
            # 同一首歌拿着相同 https 链接再进来：按上次 https 已失败的下载重试处理
            del self._https_tried[mid]
            return raw
        self._https_tried[mid] = url
        if len(self._https_tried) > self._HTTPS_FALLBACK_MAX:
            self._https_tried.clear()
            self._https_tried[mid] = url
        return url

    # song_url_best 不挂 _guard_sdk：限速按真实请求计数（_url_for/_cdn 各自 _limit），
    # 入口 acquire 只会双扣；异常面在方法体内已完整归一（BaseApiException 记账降档、
    # ValueError 单档失败、末尾 _map(last_exc)），装饰器的兜底映射无可补充
    async def song_url_best(
        self, song: dict, preferred: str = "auto", *, trial_fallback: bool = True
    ) -> dict:
        # 取流只认 songmid（sid）：sid2 是数字 songid，兜底请求必然失败，
        # 还会把「缺 mid」误归因成 VIP 问题
        mid = song.get("sid") or ""
        if not mid:
            raise ApiError("歌曲缺少 mid，无法取流", source="qq")
        ladder = ladder_for("qq", preferred)
        # 匿名 + 付费标记的歌：上游 pay 元数据可信（免费歌不标 pay），匿名对
        # 全部明文档（128/320/flac 及以上）一律 104003——逐档试只会白烧 3+ 次
        # 请求（每档还有 250ms 限速间隔）加剧匿名场景的风控。整条阶梯置空，
        # 由下方空阶梯分支按 104003 归因，直接落试听兜底（TRY 档匿名可用）
        if song.get("pay") and self._credential() is None:
            ladder = []
        type_map = {n: (p, f) for n, p, f in QQ_LADDER}
        self._warn_stale_ladder(type_map)
        last_code = None
        last_exc: BaseApiException | ApiError | None = None
        # 音质偏好钉在高档（master/atmos）时上面的匿名+付费过滤可能把阶梯滤成空：
        # 整个阶梯不执行、last_code 缺失会让归因退化成「可能无版权」。空阶梯的
        # 语义就是「匿名拿不到任何档」——按 104003 归因记账，试听兜底照常尝试
        # （ladder_for 对合法/非法档位都非空，空阶梯只可能来自该过滤）
        if not ladder:
            last_code = 104003
        for q in ladder:
            primary, fallback = type_map.get(q, (None, None))
            for ftype_name in (primary, fallback):
                if not ftype_name:
                    continue
                ftype = getattr(SongFileType, ftype_name, None)
                if ftype is None:
                    continue
                try:
                    r = await self._url_for(mid, ftype)
                except (RatelimitedError, CredentialExpiredError, TimeoutNetworkError) as e:
                    # 风控/凭证失效/网络级超时是账号级或环境级故障，降档只会逐档
                    # 重复失败（每档还有限速间隔，最坏分钟级挂起）：直接归因抛出
                    # （对齐 ncm/kg 的 e.timeout 短路、试听分支同口径）
                    raise self._map(e) from e
                except ValueError as e:
                    # pydantic ValidationError（ValueError 子类）是单档解析失败：
                    # 与 BaseApiException 同路径记账降档，否则穿出到装饰器会终止整个阶梯。
                    # 多档连续解析失败时保留最后一档的 cause（只记首档会丢后续档的归因）
                    last_exc = ApiError("QQ 音乐响应解析失败，请稍后重试", source="qq")
                    last_exc.__cause__ = e
                    continue
                except BaseApiException as e:
                    # 单档取流失败不终止整个阶梯（网络抖动/单点 404），记下来继续降档
                    last_exc = e
                    continue
                if r["ok"]:
                    cdn = await self._cdn()
                    return {
                        "url": self._stream_url(mid, r["url"], cdn),
                        "quality": q,
                        "qualityLabel": QQ_LABEL.get(q, q),
                        "trial": False,
                        "ext": (ftype.e or "").lstrip(".").lower(),
                    }
                # 只补记不覆写：高档 104003 之后某档返回空 data（code=None）时，
                # 归因不能被冲成「可能无版权」（与下方 TRY 档同口径）
                if last_code is None:
                    last_code = r["code"]
        if trial_fallback and self._config.src_quality_unblock("qq"):
            try:
                r = await self._url_for(mid, SpecialSongFileType.TRY)
                if r["ok"]:
                    cdn = await self._cdn()
                    url = self._stream_url(mid, r["url"], cdn)
                    return {
                        "url": url,
                        "quality": "try",
                        "qualityLabel": "试听片段",
                        "trial": True,
                        "ext": "mp3",
                    }
                # TRY 档 data 空数组时 code=None：只补记不覆写，否则阶梯阶段已归因的
                # 104003（需 VIP）会被冲掉，最终报错退化成「无版权或稍后重试」
                if last_code is None:
                    last_code = r["code"]
            except BaseApiException as e:
                raise self._map(e) from e
            except ValueError as e:
                # 与 _guard_sdk 同文案：试听兜底里的解析失败不该以裸 ValidationError 甩给用户
                raise ApiError("QQ 音乐响应解析失败，请稍后重试", source="qq") from e
        if last_exc is not None:
            # 阶梯全靠异常走到底：真实原因在 last_exc 里，归因它而不是套「无版权」兜底文案；
            # last_code 分支保留给「有 code 但没抛异常」的路径
            raise self._map(last_exc)
        if last_code in _NO_LOGIN_CODES:
            # 不带 code：错误码表 104003 是无试听提示的泛化文案，user_msg 优先查表
            # 会盖掉这里多出来的「可发试听」引导（对齐 kg.require_login 的处理）
            raise ApiError("该歌曲需要 QQ 音乐 VIP/登录才能获取完整链接（可发试听）", source="qq")
        raise ApiError("无法获取播放链接（可能无版权或稍后重试）", code=last_code, source="qq")

    # ──────────── 歌词 ────────────
    @_guard_sdk
    async def lyric(self, sid: str) -> dict:
        client = await self.get_client()
        res = await client.lyric.get_lyric(sid, qrc=False, trans=True)
        lrc = getattr(res, "lyric", "") or ""
        trans = getattr(res, "trans", "") or ""
        return {"lrc": lrc, "tlyric": trans, "yrc": ""}

    @_guard_sdk
    async def lyric_qrc(self, sid: str) -> dict:
        """逐字歌词：QRC XML → 行列表。

        ``lrc`` 字段恒为空串（对齐 kg 的约定：逐字结果走 yrc/lines）——
        这里拿到的是 QRC XML，把原文塞进 lrc 会让当纯文本渲染的上层吐出标签。
        """
        client = await self.get_client()
        res = await client.lyric.get_lyric(sid, qrc=True, trans=True)
        qrc = getattr(res, "lyric", "") or ""
        trans = getattr(res, "trans", "") or ""
        lines = parse_qrc(qrc)
        if not lines and "QrcInfos" not in qrc:
            # 非 XML 的裸 LRC 文本按 LRC 兜底；XML 解析失败时保持空——
            # service.fetch_lyric_karaoke 见 yrc 为空会回退普通歌词，比吐 XML 原文好
            lines = parse_lrc(qrc)
        return {"lrc": "", "tlyric": trans, "yrc": "\n".join(lines), "lines": lines}

    # ──────────── song 协议（service 零分支委托的统一入口） ────────────
    async def song_lyric(self, song: dict) -> dict:
        # 歌词接口只认 songmid（sid）：拿 sid2（songid）兜底会换来上游报错而不是
        # 干净的空歌词。缺 sid 也不能拿空串打库——库报错会被归因成「响应解析失败」，
        # 协议契约（base.SourceClient.song_lyric）要求无歌词返回空串而非抛错，
        # 与 song_comments「缺定位键不硬凑」同口径
        sid = song.get("sid", "")
        if not sid:
            return {"lrc": "", "tlyric": "", "yrc": ""}
        return await self.lyric(sid)

    async def song_lyric_karaoke(self, song: dict) -> dict:
        # 缺 sid 同 song_lyric：回空结果而不是空串打库
        sid = song.get("sid", "")
        if not sid:
            return {"lrc": "", "tlyric": "", "yrc": ""}
        return await self.lyric_qrc(sid)

    async def song_comments(self, song: dict, limit: int = 12) -> dict:
        # 评论载体与歌词不同源：评论用 songid（sid2），缺省时不猜 songmid；
        # 与 kg/ncm 同口径返回空结果而不是抛错
        if not song.get("sid2", ""):
            return {"hot": [], "new": [], "total": 0}
        return await self.comments(song.get("sid2", ""), limit)

    # ──────────── 榜单 ────────────
    @_guard_sdk
    async def new_albums(self, area: int = 1, limit: int = 15) -> list[dict]:
        client = await self.get_client()
        items = await client.album.get_new_album(area=area, num=limit).collect_items(limit)
        return [a for a in (_norm_album(x, i) for i, x in enumerate(items)) if a]

    @_guard_sdk
    async def rank_list(self) -> list[dict]:
        client = await self.get_client()
        res = await client.top.get_category()
        out = []
        idx = 0
        for group in getattr(res, "group", None) or []:
            for t in getattr(group, "toplist", None) or []:
                idx += 1
                out.append(
                    {
                        "index": idx,
                        "id": str(getattr(t, "id", 0) or ""),
                        "name": getattr(t, "name", "") or "",
                        "sub": getattr(t, "update_time", "") or getattr(t, "title", "") or "",
                        "cover": getattr(t, "head_pic_url", "") or getattr(t, "front_pic_url", "") or "",
                        "tag": getattr(group, "name", "") or "",
                    }
                )
        return out[:40]

    @_guard_sdk
    async def rank_songs(self, top_id: str, limit: int = 30) -> list[dict]:
        # 入参前置校验：int() 的 ValueError 会被 _guard_sdk 归因为「响应解析失败」，误导排障
        tid = int(top_id) if str(top_id).isdecimal() else 0
        if not tid:
            raise ApiError("榜单 id 无效", source="qq")
        client = await self.get_client()
        songs = await client.top.get_detail(top_id=tid, num=limit).collect_items(limit)
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    @_guard_sdk
    async def new_songs(self, type_: int = 5) -> list[dict]:
        client = await self.get_client()
        res = await client.recommend.get_recommend_newsong(type=type_)
        return [s for s in (_norm_song(x, i) for i, x in enumerate(getattr(res, "songs", None) or [])) if s][
            :20
        ]

    # ──────────── 歌手 / 专辑 / 歌单 ────────────
    @_guard_sdk
    async def artist_songs(self, singer_mid: str, limit: int = 30) -> list[dict]:
        client = await self.get_client()
        songs = await client.singer.get_songs_list(singer_mid, num=limit).collect_items(limit)
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    async def _singer_mid_by_keyword(self, keyword: str) -> dict | None:
        """四个歌手类 *_by_keyword 共用的第一步：按关键词搜歌手取首选，未命中 None。"""
        cands = await self.search(keyword, limit=5, type_=SearchType.SINGER)
        return cands[0] if cands else None

    async def artist_songs_by_keyword(self, keyword: str, limit: int = 30) -> tuple[dict | None, list[dict]]:
        singer = await self._singer_mid_by_keyword(keyword)
        if singer is None:
            return None, []
        return singer, await self.artist_songs(singer["id"], limit)

    @_guard_sdk
    async def artist_albums(self, singer_mid: str, limit: int = 15) -> list[dict]:
        client = await self.get_client()
        albums = await client.singer.get_album_list(singer_mid, num=limit).collect_items(limit)
        return [a for a in (_norm_album(x, i) for i, x in enumerate(albums)) if a]

    async def artist_albums_by_keyword(self, keyword: str, limit: int = 15) -> tuple[dict | None, list[dict]]:
        singer = await self._singer_mid_by_keyword(keyword)
        if singer is None:
            return None, []
        return singer, await self.artist_albums(singer["id"], limit)

    @_guard_sdk
    async def artist_mvs(self, singer_mid: str, limit: int = 10, fallback_artist: str = "") -> list[dict]:
        client = await self.get_client()
        mvs = await client.singer.get_mv_list(singer_mid, num=limit).collect_items(limit)
        # VideoBrief 无任何 singer 字段（与 MV 搜索的 MvSearch.singer_name 不同），
        # 不透传歌手名则「歌手MV」列表所有条目 artist 恒空
        return [m for m in (self._norm_mv(x, i, fallback_artist) for i, x in enumerate(mvs)) if m]

    async def artist_mvs_by_keyword(self, keyword: str, limit: int = 10) -> tuple[dict | None, list[dict]]:
        singer = await self._singer_mid_by_keyword(keyword)
        if singer is None:
            return None, []
        return singer, await self.artist_mvs(
            singer["id"], limit, fallback_artist=str(singer.get("name") or "")
        )

    @_guard_sdk
    async def similar_singers(self, singer_mid: str, limit: int = 10) -> list[dict]:
        client = await self.get_client()
        res = await client.singer.get_similar(singer_mid, number=limit)
        singers = getattr(res, "singerlist", None) or []
        return [s for s in (_norm_singer(x, i) for i, x in enumerate(singers)) if s][:limit]

    async def similar_singers_by_keyword(
        self, keyword: str, limit: int = 10
    ) -> tuple[dict | None, list[dict]]:
        singer = await self._singer_mid_by_keyword(keyword)
        if singer is None:
            return None, []
        return singer, await self.similar_singers(singer["id"], limit)

    @_guard_sdk
    async def album_songs(self, album_mid: str, limit: int = 30) -> list[dict]:
        client = await self.get_client()
        songs = await client.album.get_song(album_mid, num=limit).collect_items(limit)
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    async def _expand_or_candidates(
        self, keyword: str, st, expand: Callable[[dict], Awaitable[tuple[dict | None, list[dict]]]]
    ) -> tuple[dict | None, list[dict]]:
        """两个 *_by_keyword 共用骨架：搜不到 → 未命中；唯一命中 → 交给 expand 展开；
        多命中 → (None, 候选) 交上层出选择列表。expand 收候选项，返回 (meta, songs)。"""
        cands = await self.search(keyword, limit=8, type_=st)
        if not cands:
            return None, []
        if len(cands) == 1:
            return await expand(cands[0])
        return None, cands

    async def album_songs_by_keyword(self, keyword: str, limit: int = 30) -> tuple[dict | None, list[dict]]:
        async def expand(c):
            return c, await self.album_songs(c["id"], limit)

        return await self._expand_or_candidates(keyword, SearchType.ALBUM, expand)

    @_guard_sdk
    async def songlist_songs(self, songlist_id: str, limit: int = 30) -> tuple[dict, list[dict]]:
        # 入参前置校验：理由同 rank_songs
        pid = int(songlist_id) if str(songlist_id).isdecimal() else 0
        if not pid:
            raise ApiError("歌单 id 无效", source="qq")
        client = await self.get_client()
        res = await client.songlist.get_detail(pid, num=limit)
        info = getattr(res, "info", None)
        creator = getattr(info, "creator", None)
        meta = {
            "id": str(getattr(info, "id", 0) or songlist_id),
            "name": getattr(info, "title", "") or "",
            "cover": getattr(info, "picurl", "") or "",
            "creator": (getattr(creator, "nick", "") or "") if creator else "",
            "playCount": int(getattr(info, "listennum", 0) or 0),
            "trackCount": int(getattr(info, "songnum", 0) or 0),
        }
        songs = list_of(getattr(res, "songs", None))
        return meta, [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s][:limit]

    async def songlist_by_keyword(self, keyword: str, limit: int = 30) -> tuple[dict | None, list[dict]]:
        async def expand(c):
            return await self.songlist_songs(c["id"], limit)

        return await self._expand_or_candidates(keyword, SearchType.SONGLIST, expand)

    # ──────────── 推荐 / 随机 ────────────
    @_guard_sdk
    async def _radar_songs(self, limit: int) -> list[dict]:
        """私人雷达推荐列表。随机歌曲与日推共用同一上游接口，仅取数条数不同。"""
        client = await self.get_client()
        songs = await client.recommend.get_radar_recommend().collect_items(limit)
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    async def random_song(self) -> dict | None:
        songs = await self._radar_songs(5)
        return songs[0] if songs else None

    @_guard_sdk
    async def recommend_playlists(self) -> list[dict]:
        client = await self.get_client()
        items = await client.recommend.get_recommend_songlist().collect_items(15)
        return [p for p in (_norm_songlist(x, i) for i, x in enumerate(items)) if p]

    async def daily_recommend(self) -> list[dict]:
        """日推需登录：本地无有效凭证时直接抛 ApiError（code=1000），由上层引导扫码。"""
        if self._credential() is None:
            raise ApiError("日推需要登录，请先扫码登录 QQ 音乐", code=1000, source="qq")
        return await self._radar_songs(30)

    @_guard_sdk
    async def guess_songs(self, limit: int = 10) -> list[dict]:
        """猜你喜欢（匿名可用，Android 平台接口）。"""
        client = await self.get_client()
        res = await client.recommend.get_guess_recommend()
        songs = getattr(res, "songs", None) or []
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s][:limit]

    # ──────────── 评论 / MV ────────────
    @staticmethod
    def _norm_comment(c, i: int, hot: bool) -> dict | None:
        """评论条目 → 统一结构。index 用原始枚举序：跳过空内容时序号不重排。"""
        content = (getattr(c, "content", "") or "").strip()
        if not content:
            return None
        return {
            "index": i + 1,
            "nick": getattr(c, "nick", "") or "QQ音乐用户",
            "avatar": getattr(c, "avatar", "") or "",
            "time": _fmt_pub_time(getattr(c, "pub_time", "")),
            "likes": int(getattr(c, "praise_num", 0) or 0),
            "content": content[:300],
            "hot": hot,
        }

    @_guard_sdk
    async def comments(self, song_id: str, limit: int = 12, kind: str = "music") -> dict:
        # 与 similar_songs/rank_songs 同一前置校验：int() 的 ValueError 会被
        # _guard_sdk 误归因为「响应解析失败」，误导排障
        biz_id = int(song_id) if str(song_id).isdecimal() else 0
        if not biz_id:
            raise ApiError("歌曲缺少有效 id", source="qq")
        client = await self.get_client()
        items = await client.comment.get_hot_comments(biz_id=biz_id).collect_items(limit)
        out = [d for d in (self._norm_comment(c, i, hot=True) for i, c in enumerate(items)) if d]
        total = len(out)
        try:
            total = int(getattr(await client.comment.get_comment_count(biz_id), "count", 0) or total)
        except (BaseApiException, ValueError):
            # ValueError（pydantic ValidationError 是其子类）不拦的话会穿透到
            # _guard_sdk，让已经成功取得的热评整体变成「响应解析失败」
            pass
        return {"hot": out, "new": [], "total": total}

    @_guard_sdk
    async def new_comments(self, song_id: str, limit: int = 12) -> list[dict]:
        """最新评论（「评论下页」用）。"""
        biz_id = int(song_id) if str(song_id).isdecimal() else 0
        if not biz_id:
            raise ApiError("歌曲缺少有效 id", source="qq")
        client = await self.get_client()
        items = await client.comment.get_new_comments(biz_id=biz_id, page_size=limit).collect_items(limit)
        return [d for d in (self._norm_comment(c, i, hot=False) for i, c in enumerate(items)) if d]

    @_guard_sdk
    async def mv_urls(self, vid: str) -> dict:
        client = await self.get_client()
        res = await client.mv.get_mv_urls([vid])
        data = getattr(res, "data", None) or {}
        node = data.get(vid) if isinstance(data, dict) else None
        url = ""
        if node:
            mp4 = getattr(node, "mp4", None) or []
            for item in mp4:
                for u in getattr(item, "url", None) or []:
                    if u:
                        url = str(u).replace("http://", "https://")
                        break
                if url:
                    break
        return {"url": url, "vid": vid}

    async def song_mv_url(self, song: dict) -> dict:
        """统一协议：取 MV 直链；无 mvid 返回空。"""
        vid = song.get("mvid")
        if not vid:
            return {"url": ""}
        return await self.mv_urls(str(vid))

    @_guard_sdk
    async def mv_search(self, keyword: str, limit: int = 8) -> list[dict]:
        """MV 搜索（与 ncm/kg 同名同契约，返回结构见 _norm_mv）。

        此前只有 ncm/kg 有该方法，「MV搜」在仅 QQ 部署 / qq 默认音源下会
        AttributeError 崩到兜底文案；播放链路（mv_urls）本就齐全，补搜索即可。
        """
        return await self.search(keyword, limit, type_=SearchType.MV)

    # ──────────── 登录态 ────────────
    async def login_status(self) -> dict:
        """本地登录态摘要。loggedIn 只代表凭证未过本地有效期（is_expired 读本地
        时间戳），服务端吊销的凭证这里仍显示已登录；真正失效会在取流时暴露。"""
        cred = self._credential()
        if cred is None:
            return {"loggedIn": False, "uid": "", "nickname": "", "avatar": ""}
        uin = str(getattr(cred, "musicid", "") or "")
        prefix = str(getattr(cred, "musickey", "") or "")
        # 库自身推断微信登录用的是 startswith("W_X") 3 字符——第 4 位不保证是下划线，
        # 比 4 字符会把部分微信账号显示成「QQ 账号」
        kind = "微信" if prefix.startswith("W_X") else "QQ"
        expired = False
        try:
            expired = bool(cred.is_expired())
        except Exception:
            pass
        if expired and not int(getattr(cred, "musickey_create_time", 0) or 0):
            # 库的 is_expired 用 create_time + expires_in 与当前时间比较，两字段缺省 0
            # ——手工导入的最小凭证 JSON（只有 musicid/musickey）会被恒判过期，
            # 显示「未登录」但取流实际可用。无本地签发时间戳时交给服务端判定
            # （真正失效会在取流时暴露），不在这里下假阴性结论
            expired = False
        return {
            "loggedIn": not expired,
            "uid": uin,
            "nickname": f"{kind} 账号 {uin[-4:] if len(uin) > 4 else uin}",
            "avatar": "",
        }

    @_guard_sdk
    async def vip_info(self) -> dict:
        client = await self.get_client()
        if self._credential() is None:
            return {"vipLevel": 0, "expire": ""}
        try:
            res = await client.user.get_vip_info()
            # 字段以 UserVipInfoResponse 为准：identity.level 会员等级，
            # userinfo.expire 到期时间戳（秒/毫秒都容），huge_vip_end 作兜底
            identity = getattr(res, "identity", None)
            userinfo = getattr(res, "userinfo", None)
            level = int(getattr(identity, "level", 0) or 0)
            expire = ""
            ts = int(getattr(userinfo, "expire", 0) or 0)
            if ts > 0:
                if ts > 10**12:
                    ts /= 1000
                expire = time.strftime("%Y-%m-%d", time.localtime(ts))
            if not expire:
                expire = str(getattr(identity, "huge_vip_end", "") or "")[:10]
            return {"vipLevel": level, "expire": expire}
        except BaseApiException:
            return {"vipLevel": 0, "expire": ""}

    def _require_login(self):
        """返回 (client, credential)；未登录抛 ApiError。"""
        cred = self._credential()
        if cred is None:
            raise ApiError("需要登录 QQ 音乐账号", code=1000, source="qq")
        return cred

    def _euin(self) -> str:
        """加密 uin：关注/收藏列表接口都要它，扫码凭证里自带。"""
        cred = self._credential()
        return str(getattr(cred, "encrypt_uin", "") or "") if cred else ""

    def _require_euin(self) -> str:
        """收藏/关注列表接口共用的前置：未登录或凭证缺加密 uin 都在发请求前拦下。"""
        self._require_login()
        euin = self._euin()
        if not euin:
            raise ApiError("凭证缺少加密 uin，请退出后重新扫码登录", source="qq")
        return euin

    @_guard_sdk
    async def fav_songs(self, limit: int = 30) -> list[dict]:
        """「我喜欢」红心列表（dirid=201）。"""
        client = await self.get_client()
        euin = self._require_euin()
        songs = await client.user.get_fav_song(euin, num=min(limit, 50)).collect_items(limit)
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    @_guard_sdk
    async def created_songlists(self) -> list[dict]:
        client = await self.get_client()
        cred = self._require_login()
        uin = _safe_int(getattr(cred, "musicid", 0))
        res = await client.user.get_created_songlist(uin)
        return [
            p
            for p in (_norm_songlist(x, i) for i, x in enumerate(getattr(res, "playlists", None) or []))
            if p
        ]

    @_guard_sdk
    async def fav_songlists(self, limit: int = 30) -> list[dict]:
        """收藏的外部歌单。"""
        client = await self.get_client()
        euin = self._require_euin()
        pls = await client.user.get_fav_songlist(euin, num=min(limit, 50)).collect_items(limit)
        return [p for p in (_norm_songlist(x, i) for i, x in enumerate(pls)) if p]

    @_guard_sdk
    async def follow_singers(self, limit: int = 30) -> list[dict]:
        client = await self.get_client()
        euin = self._require_euin()
        users = await client.user.get_follow_singers(euin, num=min(limit, 50)).collect_items(limit)
        return [
            {
                "index": i + 1,
                "id": getattr(u, "mid", "") or "",
                "name": getattr(u, "name", "") or "",
                "cover": getattr(u, "avatar_url", "") or "",
                "sub": f"{_safe_int(getattr(u, 'fan_num', 0))} 粉丝",
            }
            for i, u in enumerate(users[:limit])
            if getattr(u, "name", "")
        ]

    @_guard_sdk
    async def like_toggle(self, song: dict, like: bool) -> str:
        """红心 / 取消红心（写「我喜欢」歌单，dirid=201）。"""
        client = await self.get_client()
        self._require_login()
        raw_id = str(song.get("sid2") or "")
        song_id = int(raw_id) if raw_id.isdecimal() else 0
        if not song_id:
            raise ApiError("这首歌缺少歌曲 id，无法红心", source="qq")
        if like:
            ok = await client.songlist.like_song([(song_id, 0)])
        else:
            ok = await client.songlist.unlike_song([(song_id, 0)])
        # 库的 like/unlike 幂等（歌曲已在/不在「我喜欢」也返回 True），False 只在
        # 上游拒绝（retCode 非 0）时出现，此分支基本不可达，仅作兜底
        if ok:
            return "已加入红心 ❤" if like else "已取消红心"
        return "操作未生效（可能已是该状态）"

    @_guard_sdk
    async def refresh_credential(self) -> bool:
        client = await self.get_client()
        cred = self._credential()
        if cred is None:
            return False
        try:
            new_cred = await client.login.refresh_credential(cred)
            data = new_cred.model_dump(by_alias=True)
            self._config.set_src_cookie(
                "qq", json.dumps(data, ensure_ascii=False), str(data.get("musicid") or "")
            )
            saved = await self._config.save_async()
            # 刷新后的 musickey 已变，必须让缓存失效，否则 5 秒内仍用旧凭证
            self.invalidate_credential()
            # 库的 refresh 只在「不传 credential」时才回写 client（None 分支），插件恒传
            # 显式 cred → 永不回写；不补这一行，保活写进配置的新 key 到不了活客户端
            if self._client is not None:
                self._client.credential = new_cred
            return saved
        except BaseApiException as e:
            # 原文可能带内部信息不上抛，但类型留痕：scheduler 只显示「失败（需重新
            # 扫码）」，保活连败时无类型线索无法区分风控/网络/凭证失效
            logger.warning(f"[music_hub] QQ 凭证刷新失败：{type(e).__name__}")
            return False

    async def logout(self) -> None:
        self._config.clear_src_cookie("qq")
        self.invalidate_credential()
        # client 上残留的 credential 也要清，否则匿名判定会看到旧登录态
        if self._client is not None:
            self._client.credential = None
        await self._config.save_async()

    # ---- 扫码登录（供 WebUI / 聊天指令共用）----
    @_guard_sdk
    async def qr_start(self, login_type: str = "qq") -> dict:
        """创建扫码会话。返回 {session, qrB64}；session 由 LoginFlows 持有并后台消费。"""
        if not available():
            raise NotEnabledError("qqmusic-api-python 未安装", source="qq")
        client = await self.get_client()
        lt = {
            "qq": QRLoginType.QQ,
            "wx": QRLoginType.WX,
            "微信": QRLoginType.WX,
            "mobile": QRLoginType.MOBILE,
            "app": QRLoginType.MOBILE,
        }.get(login_type, QRLoginType.QQ)
        session = QRCodeLoginSession(client.login, lt, interval=1.5, timeout_seconds=180.0)
        qr = await session.get_qrcode()
        data = getattr(qr, "data", b"") or b""
        return {"session": session, "qrB64": base64.b64encode(data).decode() if data else ""}

    async def qr_consume(self, session, on_event) -> None:
        """完整消费扫码事件流（后台任务里跑）。on_event(state: str, credential_json: str)。

        state: scanned / done / timeout / refuse

        iter_events() 产出的是 QRLoginResult（字段 .event / .credential）而非枚举本身；
        DONE 事件自带 credential 直接取用；期间不要再调 wait_qrcode_login()（会另起
        一个并发轮询流）。
        """

        async def _emit(state: str, detail: str = "") -> None:
            # 回调通道（WebUI 推送/聊天回复）自身故障不该落进下面的事件流 except——
            # 那会把一次内部故障误报成「登录超时/被拒绝」；凭证在 done 前已落缓存，
            # 回调失败也不影响登录事实
            try:
                await on_event(state, detail)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[music_hub] QQ 扫码回调异常: {e}")

        try:
            async for result in session.iter_events():
                ev = getattr(result, "event", None)
                if ev in (QRCodeLoginEvents.SCAN, QRCodeLoginEvents.CONF):
                    await _emit("scanned")
                elif ev == QRCodeLoginEvents.DONE:
                    credential = getattr(result, "credential", None)
                    if credential is None:
                        await _emit("refuse", "登录结果缺少凭证")
                        return
                    data = credential.model_dump(by_alias=True)
                    self._cred_cache = (time.monotonic(), credential)
                    # QQ/微信/MOBILE 三条扫码授权路径库都不回写 client.credential（仅
                    # 手机验证码 phone_authorize 会）；不显式回写，换号重扫后取流仍走旧账号
                    if self._client is not None:
                        self._client.credential = credential
                    await _emit("done", json.dumps(data, ensure_ascii=False))
                    return
                elif ev == QRCodeLoginEvents.TIMEOUT:
                    await _emit("timeout")
                    return
                elif ev == QRCodeLoginEvents.REFUSE:
                    await _emit("refuse")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 事件流异常（超时/网络）
            # 库内部异常文本可能带路径/域名细节，不透给登录界面；分类给固定文案，细节落日志
            msg = str(e)
            logger.warning(f"[music_hub] QQ 扫码登录事件流异常: {e}")
            await _emit("timeout" if ("超时" in msg or "timeout" in msg.lower()) else "refuse")

    # ──────────── 异常映射 ────────────
    @staticmethod
    def _map(e: BaseException) -> ApiError:
        """按异常类型映射，不靠匹配 message 字符串。

        库各异常的默认文案并不统一（CredentialExpiredError 是"登录凭证已过期, 请重新登录"，
        不含任何可匹配的中文关键词），按字符串匹配会让过期凭证走兜底分支、
        把英文原文透给用户。
        """
        # song_url_best 等编排路径会把已归一的 ApiError 记为 last_exc 再走这里：
        # 直通返回，避免 str(ApiError) 的类型前缀被再包一层
        if isinstance(e, ApiError):
            return e
        if isinstance(e, CredentialInvalidError):
            # 未提供/损坏凭证（库在请求前抛，code=None）：不再靠文案关键字命中
            return ApiError("需要登录 QQ 音乐账号", code=getattr(e, "code", None), source="qq")
        if isinstance(e, CredentialExpiredError):
            return ApiError("QQ 登录凭证已过期，请重新扫码登录", code=getattr(e, "code", None), source="qq")
        if isinstance(e, RatelimitedError):
            hint = str(getattr(e, "feedback_url", "") or "")
            suffix = f"（{hint}）" if hint else ""
            return ApiError(
                f"QQ 音乐触发风控，请稍后再试{suffix}", code=getattr(e, "code", None), source="qq"
            )
        if isinstance(e, TimeoutNetworkError):
            # 连接/读取超时：timeout=True 让上层重试链路按「环境级故障」处理，
            # 阶梯也会短路（song_url_best 的 except 元组），不再逐档白烧
            return ApiError(
                "QQ 音乐请求超时，请稍后重试", code=getattr(e, "code", None), source="qq", timeout=True
            )
        if isinstance(e, NetworkError):
            return ApiError("QQ 音乐网络异常，请稍后重试", code=getattr(e, "code", None), source="qq")
        if isinstance(e, HTTPError):
            # 兜底分支会让 "HTTP 500: ..." 原文透给用户
            return ApiError(
                f"QQ 音乐服务异常（HTTP {getattr(e, 'status_code', '') or '?'}）",
                code=getattr(e, "code", None),
                source="qq",
            )
        if isinstance(e, ApiDataError):
            return ApiError("QQ 音乐响应解析失败，请稍后重试", code=getattr(e, "code", None), source="qq")
        msg = str(e)
        if "需要登录" in msg or "未提供有效" in msg:
            return ApiError("需要登录 QQ 音乐账号", code=getattr(e, "code", None), source="qq")
        if "风控" in msg:
            return ApiError("触发风控，请稍后再试", code=getattr(e, "code", None), source="qq")
        return ApiError(msg, code=getattr(e, "code", None), source="qq")


if available():  # 依赖缺失时不注册：create() 不会拿到一个必然失败的实例

    def _create(config, qq_device_path=None, **kw):
        # device_path 是必传 kwarg（service 恒传）：漏传会在 get_client 里
        # self._device_path.parent 抛 AttributeError、不在 BaseApiException
        # 捕获面——入口显式拒绝，报错可读
        if not qq_device_path:
            raise NotEnabledError("QQ 音源缺少设备指纹路径（qq_device_path）", source="qq")
        return QQClient(config, qq_device_path)

    register("qq", _create)
