"""QQ音乐音源客户端（基于 qqmusic-api-python，进程内直连官方接口）。

- 单例 ``Client``：设备指纹落盘（data/plugin_data/<plugin>/qq_device.json），
  避免每次重启都是新设备触发风控
- 登录态：``Credential`` JSON 存配置 ``qq.cookie``（扫码登录自动写入）
- 取流：CDN dispatch + get_song_urls 按音质阶梯尝试；匿名 VIP 歌 104003，
  免费歌可用；试听档 SpecialSongFileType.TRY 匿名可用
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..errors import ApiError, NotEnabledError
from ..quality import QQ_LABEL, QQ_LADDER, ladder_for
from .http import list_of
from .registry import register

# qqmusic-api 是可选重依赖：缺失时给出清晰指引而不是 import 崩溃
try:
    from qqmusic_api import Client, Credential
    from qqmusic_api.core.exceptions import (
        BaseApiException,
        CredentialExpiredError,
        NetworkError,
        RatelimitedError,
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


def _guard_sdk(fn):
    """SDK 调用统一出口：BaseApiException → _map，ValidationError → 固定文案 ApiError。

    库对上游报文的 model_validate 无任何包装，pydantic.ValidationError（ValueError
    子类）不是 BaseApiException，会穿透所有 `except BaseApiException` 调用点，把
    "1 validation error for ..." 原文甩给用户；在出口统一兜住，方法体不必逐个加 try。
    """

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        try:
            return await fn(self, *args, **kwargs)
        except BaseApiException as e:
            raise self._map(e) from e
        except ValueError as e:
            raise ApiError("QQ 音乐响应解析失败，请稍后重试", source="qq") from e

    return wrapper


def _norm_song(item, idx: int = 0) -> dict | None:
    """Song/SongSearch 模型 → 统一歌曲结构。"""
    if item is None:
        return None
    name = (getattr(item, "name", "") or getattr(item, "title", "") or "").strip()
    if not name:
        return None
    singers = getattr(item, "singer", None) or []
    artist = " / ".join(s.name for s in singers if getattr(s, "name", ""))
    album = getattr(item, "album", None)
    album_name = getattr(album, "name", "") if album else ""
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
    title = (getattr(item, "title", "") or getattr(item, "name", "") or "").strip()
    if not title:
        return None
    # 创建者昵称字段三种模型不同名：搜索 SongListSearch.nickname /
    # 推荐 RecommendSonglistItem.creator_nick / 自建 UserPlaylistSummary.nick
    creator = (
        getattr(item, "nickname", "") or getattr(item, "creator_nick", "") or getattr(item, "nick", "") or ""
    )
    return {
        "index": idx + 1,
        "id": str(getattr(item, "dirid", 0) or getattr(item, "id", 0) or ""),
        "name": title,
        "cover": getattr(item, "picurl", "") or "",
        "trackCount": int(getattr(item, "songnum", 0) or 0),
        "playCount": int(getattr(item, "listennum", 0) or 0),
        "creator": creator,
    }


def _norm_album(item, idx: int = 0) -> dict | None:
    name = (getattr(item, "name", "") or getattr(item, "title", "") or "").strip()
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
        artist = " / ".join(s.name for s in structured if getattr(s, "name", ""))
    elif isinstance(raw_singer, str):
        artist = re.sub(r"</?em>", "", raw_singer).strip()
    else:
        artist = str(getattr(item, "singer_name", "") or "")
    return {
        "index": idx + 1,
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
    name = (getattr(item, "name", "") or "").strip()
    if not name:
        return None
    mid = getattr(item, "mid", "") or ""
    return {
        "index": idx + 1,
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


def parse_qrc(qrc_xml: str) -> list[str]:
    """QRC XML（逐字歌词）→ 行文本列表。失败返回空列表。"""
    if not qrc_xml or "QrcInfos" not in qrc_xml:
        return []
    lines = []
    for m in re.finditer(r'<Line_[^>]*txt="([^"]*)"[^>]*>', qrc_xml):
        txt = m.group(1).strip()
        if txt:
            lines.append(txt)
    if not lines:
        for m in re.finditer(r"<Line_[^>]*>(.*?)</Line_", qrc_xml, re.S):
            txt = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            if txt:
                lines.append(txt)
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
        txt = re.sub(r"\[\d{1,3}:\d{1,3}(?:[.:]\d{1,3})?\]", "", txt).strip()
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

    def ready(self) -> bool:
        """重依赖是否就绪。与 ncm/kg 保持同为方法（不是 property），
        否则契约自检的 callable 判定会把它误判为「未实现」。"""
        return available()

    _CRED_TTL = 5.0

    def _credential(self, force: bool = False):
        if not available():
            return None
        ts, cached = self._cred_cache
        if not force and cached is not None and time.monotonic() - ts < self._CRED_TTL:
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
        elif force:
            return None
        return cred if cred is not None else cached

    def invalidate_credential(self) -> None:
        """登录态变更后主动失效缓存（扫码成功 / 登出 / 刷新失败）。"""
        self._cred_cache = (0.0, None)

    @_guard_sdk
    async def get_client(self):
        """懒加载单例 Client（带登录态）。未安装库抛 NotEnabledError。"""
        if not available():
            raise NotEnabledError(
                f"qqmusic-api-python 未安装：pip install qqmusic-api-python（{import_error()}）", source="qq"
            )
        async with self._lock:
            if self._client is None:
                try:
                    self._device_path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                self._client = Client(device_path=str(self._device_path))
                await self._client.__aenter__()
            # 仅在 client 尚无有效登录态时补一次注入。库不会替插件维护 client 上的
            # 凭证：只有 refresh 不传参 / phone_authorize / logout 三个路径会回写，
            # 插件的 refresh 恒传显式 cred、QQ/微信/MOBILE 扫码路径（qr_consume）
            # 库也不回写——那两处成功后由插件显式回写（见对应方法）。
            # 这里同样不能每次调用都重写：会把刚拿到/刚刷新的有效凭证冲掉。
            # 判据必须是 musicid/musickey，不能是 `credential is None`——
            # qqmusic_api 的 setter 是 `value or Credential()`，None 会被兜底成空凭证，
            # 判 None 永远为假，配置里的凭证在插件重启后永远注入不进来。
            cred = getattr(self._client, "credential", None)
            if not (cred and cred.musicid and cred.musickey):
                self._client.credential = await asyncio.to_thread(self._credential)
            return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

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
            st = self.search_type(st) or SearchType.SONG
        try:
            res = await client.search.search_by_type(keyword=keyword, search_type=st, num=limit, page=page)
        except BaseApiException as e:
            raise self._map(e) from e
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
    def _norm_mv(item, idx: int = 0) -> dict | None:
        name = (getattr(item, "name", "") or getattr(item, "title", "") or "").strip()
        if not name:
            return None
        vid = getattr(item, "vid", "") or ""
        singers = getattr(item, "singer", None) or []
        if not singers:
            sname = getattr(item, "singer_name", "")
            artist = sname or ""
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
        try:
            res = await client.song.get_detail(sid)
            track = getattr(res, "track", None)
            return _norm_song(track)
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def similar_songs(self, song_id: str, limit: int = 10) -> list[dict]:
        # 上游按数字 songid 查相似；actions 层会传 sid2 或回退 sid(mid)，mid 非数字时
        # 裸 int() 抛的 ValueError 不在任何 except 面里，这里前置归一成明确报错
        song_id = int(song_id) if str(song_id).isdecimal() else 0
        if not song_id:
            raise ApiError("歌曲缺少有效 id", source="qq")
        client = await self.get_client()
        try:
            res = await client.song.get_similar_song(song_id)
            # 返回 .song 是 SimilarSongGroup 分组列表，歌曲在各组的 .song 里
            groups = getattr(res, "song", None) or []
            songs: list = []
            for g in groups:
                songs.extend(getattr(g, "song", None) or [])
            return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s][:limit]
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def other_versions(self, song: dict, limit: int = 15) -> list[dict]:
        """同曲其他版本（Live/伴奏/重录…）。参数两可：song id 或 mid。"""
        client = await self.get_client()
        value = int(song.get("sid2") or 0) or song.get("sid") or ""
        if not value:
            return []
        try:
            res = await client.song.get_other_version(value)
            songs = getattr(res, "data", None) or []
            return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s][:limit]
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def suggest(self, keyword: str) -> list[dict]:
        """搜索联想（quick_search HTTP 接口）。"""
        client = await self.get_client()
        try:
            res = await client.search.quick_search(keyword)
        except BaseApiException as e:
            raise self._map(e) from e
        out = []
        for key in ("song", "singer", "album"):
            cat = getattr(res, key, None)
            for it in getattr(cat, "itemlist", None) or []:
                name = (getattr(it, "name", "") or "").strip()
                if not name:
                    continue
                out.append({"name": name, "sub": getattr(it, "singer", "") or "", "kind": key})
        return out[:10]

    @_guard_sdk
    async def hot_search(self) -> list[dict]:
        client = await self.get_client()
        try:
            res = await client.search.get_hotkey()
            return [
                {"index": i + 1, "word": it.query or "", "hot": it.score or ""}
                for i, it in enumerate(getattr(res, "vec_hotkey", None) or [])
                if it.query
            ][:15]
        except BaseApiException as e:
            raise self._map(e) from e

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
            res = await client.song.get_cdn_dispatch()
            sip = getattr(res, "sip", None) or []
            if sip:
                base = str(sip[0])
                self._cdn_cache = (time.monotonic(), base)
                return base
        except BaseApiException:
            pass
        return "https://isure.stream.qqmusic.qq.com/"

    async def _url_for(self, mid: str, ftype) -> dict:
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

    # _url_for / _cdn 不加 _guard_sdk：前者抛的 BaseApiException 要保持裸类型，
    # song_url_best 靠它做单档失败降档；两者漏网的 ValueError 由 song_url_best 的
    # 装饰器兜住
    @_guard_sdk
    async def song_url_best(
        self, song: dict, preferred: str = "auto", *, trial_fallback: bool = True
    ) -> dict:
        mid = song.get("sid") or song.get("sid2") or ""
        if not mid:
            raise ApiError("歌曲缺少 mid，无法取流", source="qq")
        ladder = ladder_for("qq", preferred)
        type_map = {n: (p, f) for n, p, f in QQ_LADDER}
        last_code = None
        last_exc: BaseApiException | None = None
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
                except (RatelimitedError, CredentialExpiredError) as e:
                    # 风控/凭证失效是账号级故障，降档只会逐档重复失败：直接归因抛出（对齐试听分支）
                    raise self._map(e) from e
                except BaseApiException as e:
                    # 单档取流失败不终止整个阶梯（网络抖动/单点 404），记下来继续降档
                    last_exc = e
                    continue
                if r["ok"]:
                    cdn = await self._cdn()
                    url = r["url"] if r["url"].startswith("http") else cdn + r["url"]
                    return {
                        "url": url,
                        "quality": q,
                        "qualityLabel": QQ_LABEL.get(q, q),
                        "trial": False,
                        "ext": ftype.e,
                    }
                last_code = r["code"]
        if trial_fallback and self._config.src_quality_unblock("qq"):
            try:
                r = await self._url_for(mid, SpecialSongFileType.TRY)
                if r["ok"]:
                    cdn = await self._cdn()
                    url = r["url"] if r["url"].startswith("http") else cdn + r["url"]
                    return {
                        "url": url,
                        "quality": "try",
                        "qualityLabel": "试听片段",
                        "trial": True,
                        "ext": ".mp3",
                    }
                last_code = r["code"]
            except BaseApiException as e:
                raise self._map(e) from e
        if last_exc is not None:
            # 阶梯全靠异常走到底：真实原因在 last_exc 里，归因它而不是套「无版权」兜底文案；
            # last_code 分支保留给「有 code 但没抛异常」的路径
            raise self._map(last_exc)
        if last_code in _NO_LOGIN_CODES:
            raise ApiError(
                "该歌曲需要 QQ 音乐 VIP/登录才能获取完整链接（可发试听）", code=last_code, source="qq"
            )
        raise ApiError("无法获取播放链接（可能无版权或稍后重试）", code=last_code, source="qq")

    # ──────────── 歌词 ────────────
    @_guard_sdk
    async def lyric(self, sid: str) -> dict:
        client = await self.get_client()
        try:
            res = await client.lyric.get_lyric(sid, qrc=False, trans=True)
            lrc = getattr(res, "lyric", "") or ""
            trans = getattr(res, "trans", "") or ""
            return {"lrc": lrc, "tlyric": trans, "yrc": ""}
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def lyric_qrc(self, sid: str) -> dict:
        """逐字歌词：QRC XML → 行列表。"""
        client = await self.get_client()
        try:
            res = await client.lyric.get_lyric(sid, qrc=True, trans=True)
            qrc = getattr(res, "lyric", "") or ""
            trans = getattr(res, "trans", "") or ""
            lines = parse_qrc(qrc)
            if not lines:
                lines = parse_lrc(qrc)
            return {"lrc": qrc, "tlyric": trans, "yrc": "\n".join(lines), "lines": lines}
        except BaseApiException as e:
            raise self._map(e) from e

    # ──────────── song 协议（service 零分支委托的统一入口） ────────────
    async def song_lyric(self, song: dict) -> dict:
        return await self.lyric(song.get("sid") or song.get("sid2", ""))

    async def song_lyric_karaoke(self, song: dict) -> dict:
        return await self.lyric_qrc(song.get("sid") or song.get("sid2", ""))

    async def song_comments(self, song: dict, limit: int = 12) -> dict:
        # 评论载体与歌词不同源：评论用 songid（sid2），缺省时不猜 songmid
        return await self.comments(song.get("sid2", ""), limit)

    # ──────────── 榜单 ────────────
    @_guard_sdk
    async def new_albums(self, area: int = 1, limit: int = 15) -> list[dict]:
        client = await self.get_client()
        try:
            items = await client.album.get_new_album(area=area, num=limit).collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
        return [a for a in (_norm_album(x, i) for i, x in enumerate(items)) if a]

    @_guard_sdk
    async def rank_list(self) -> list[dict]:
        client = await self.get_client()
        try:
            res = await client.top.get_category()
        except BaseApiException as e:
            raise self._map(e) from e
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
        client = await self.get_client()
        try:
            songs = await client.top.get_detail(top_id=int(top_id), num=limit).collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    @_guard_sdk
    async def new_songs(self, type_: int = 5) -> list[dict]:
        client = await self.get_client()
        try:
            res = await client.recommend.get_recommend_newsong(type=type_)
        except BaseApiException as e:
            raise self._map(e) from e
        return [s for s in (_norm_song(x, i) for i, x in enumerate(getattr(res, "songs", None) or [])) if s][
            :20
        ]

    # ──────────── 歌手 / 专辑 / 歌单 ────────────
    @_guard_sdk
    async def artist_songs(self, singer_mid: str, limit: int = 30) -> list[dict]:
        client = await self.get_client()
        try:
            songs = await client.singer.get_songs_list(singer_mid, num=limit).collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
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
        try:
            albums = await client.singer.get_album_list(singer_mid, num=limit).collect_items(limit)
            return [a for a in (_norm_album(x, i) for i, x in enumerate(albums)) if a]
        except BaseApiException as e:
            raise self._map(e) from e

    async def artist_albums_by_keyword(self, keyword: str, limit: int = 15) -> tuple[dict | None, list[dict]]:
        singer = await self._singer_mid_by_keyword(keyword)
        if singer is None:
            return None, []
        return singer, await self.artist_albums(singer["id"], limit)

    @_guard_sdk
    async def artist_mvs(self, singer_mid: str, limit: int = 10) -> list[dict]:
        client = await self.get_client()
        try:
            mvs = await client.singer.get_mv_list(singer_mid, num=limit).collect_items(limit)
            return [m for m in (self._norm_mv(x, i) for i, x in enumerate(mvs)) if m]
        except BaseApiException as e:
            raise self._map(e) from e

    async def artist_mvs_by_keyword(self, keyword: str, limit: int = 10) -> tuple[dict | None, list[dict]]:
        singer = await self._singer_mid_by_keyword(keyword)
        if singer is None:
            return None, []
        return singer, await self.artist_mvs(singer["id"], limit)

    @_guard_sdk
    async def similar_singers(self, singer_mid: str, limit: int = 10) -> list[dict]:
        client = await self.get_client()
        try:
            res = await client.singer.get_similar(singer_mid, number=limit)
            singers = getattr(res, "singerlist", None) or []
            return [s for s in (_norm_singer(x, i) for i, x in enumerate(singers)) if s][:limit]
        except BaseApiException as e:
            raise self._map(e) from e

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
        try:
            songs = await client.album.get_song(album_mid, num=limit).collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
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
        client = await self.get_client()
        try:
            res = await client.songlist.get_detail(int(songlist_id), num=limit)
        except BaseApiException as e:
            raise self._map(e) from e
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
        try:
            songs = await client.recommend.get_radar_recommend().collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
        return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]

    async def random_song(self) -> dict | None:
        songs = await self._radar_songs(5)
        return songs[0] if songs else None

    @_guard_sdk
    async def recommend_playlists(self) -> list[dict]:
        client = await self.get_client()
        try:
            items = await client.recommend.get_recommend_songlist().collect_items(15)
        except BaseApiException as e:
            raise self._map(e) from e
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
        try:
            res = await client.recommend.get_guess_recommend()
        except BaseApiException as e:
            raise self._map(e) from e
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
        client = await self.get_client()
        try:
            items = await client.comment.get_hot_comments(biz_id=int(song_id or 0)).collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
        out = [d for d in (self._norm_comment(c, i, hot=True) for i, c in enumerate(items)) if d]
        total = len(out)
        try:
            total = int(
                getattr(await client.comment.get_comment_count(int(song_id or 0)), "count", 0) or total
            )
        except BaseApiException:
            pass
        return {"hot": out, "new": [], "total": total}

    @_guard_sdk
    async def new_comments(self, song_id: str, limit: int = 12) -> list[dict]:
        """最新评论（「评论下页」用）。"""
        client = await self.get_client()
        try:
            items = await client.comment.get_new_comments(
                biz_id=int(song_id or 0), page_size=limit
            ).collect_items(limit)
        except BaseApiException as e:
            raise self._map(e) from e
        return [d for d in (self._norm_comment(c, i, hot=False) for i, c in enumerate(items)) if d]

    @_guard_sdk
    async def mv_urls(self, vid: str) -> dict:
        client = await self.get_client()
        try:
            res = await client.mv.get_mv_urls([vid])
        except BaseApiException as e:
            raise self._map(e) from e
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

    # ──────────── 登录态 ────────────
    async def login_status(self) -> dict:
        """本地登录态摘要。loggedIn 只代表凭证未过本地有效期（is_expired 读本地
        时间戳），服务端吊销的凭证这里仍显示已登录；真正失效会在取流时暴露。"""
        cred = self._credential()
        if cred is None:
            return {"loggedIn": False, "uid": "", "nickname": "", "avatar": ""}
        uin = str(getattr(cred, "musicid", "") or "")
        prefix = str(getattr(cred, "musickey", "") or "")[:4]
        kind = "微信" if prefix == "W_X_" else "QQ"
        expired = False
        try:
            expired = bool(cred.is_expired())
        except Exception:
            pass
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
        try:
            songs = await client.user.get_fav_song(euin, num=min(limit, 50)).collect_items(limit)
            return [s for s in (_norm_song(x, i) for i, x in enumerate(songs)) if s]
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def created_songlists(self) -> list[dict]:
        client = await self.get_client()
        cred = self._require_login()
        uin = int(getattr(cred, "musicid", 0) or 0)
        try:
            res = await client.user.get_created_songlist(uin)
            return [
                p
                for p in (_norm_songlist(x, i) for i, x in enumerate(getattr(res, "playlists", None) or []))
                if p
            ]
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def fav_songlists(self, limit: int = 30) -> list[dict]:
        """收藏的外部歌单。"""
        client = await self.get_client()
        euin = self._require_euin()
        try:
            pls = await client.user.get_fav_songlist(euin, num=min(limit, 50)).collect_items(limit)
            return [p for p in (_norm_songlist(x, i) for i, x in enumerate(pls)) if p]
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def follow_singers(self, limit: int = 30) -> list[dict]:
        client = await self.get_client()
        euin = self._require_euin()
        try:
            users = await client.user.get_follow_singers(euin, num=min(limit, 50)).collect_items(limit)
            return [
                {
                    "index": i + 1,
                    "id": getattr(u, "mid", "") or "",
                    "name": getattr(u, "name", "") or "",
                    "cover": getattr(u, "avatar_url", "") or "",
                    "sub": f"{int(getattr(u, 'fan_num', 0) or 0)} 粉丝",
                }
                for i, u in enumerate(users[:limit])
                if getattr(u, "name", "")
            ]
        except BaseApiException as e:
            raise self._map(e) from e

    @_guard_sdk
    async def like_toggle(self, song: dict, like: bool) -> str:
        """红心 / 取消红心（写「我喜欢」歌单，dirid=201）。"""
        client = await self.get_client()
        self._require_login()
        song_id = int(song.get("sid2") or 0)
        if not song_id:
            raise ApiError("这首歌缺少歌曲 id，无法红心", source="qq")
        try:
            if like:
                ok = await client.songlist.like_song([(song_id, 0)])
            else:
                ok = await client.songlist.unlike_song([(song_id, 0)])
            if ok:
                return "已加入红心 ❤" if like else "已取消红心"
            return "操作未生效（可能已是该状态）"
        except BaseApiException as e:
            raise self._map(e) from e

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
        except BaseApiException:
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
        import base64

        return {"session": session, "qrB64": base64.b64encode(data).decode() if data else ""}

    async def qr_consume(self, session, on_event) -> None:
        """完整消费扫码事件流（后台任务里跑）。on_event(state: str, credential_json: str)。

        state: scanned / done / timeout / refuse

        iter_events() 产出的是 QRLoginResult（字段 .event / .credential）而非枚举本身；
        DONE 事件自带 credential 直接取用；期间不要再调 wait_qrcode_login()（会另起
        一个并发轮询流）。
        """
        try:
            async for result in session.iter_events():
                ev = getattr(result, "event", None)
                if ev in (QRCodeLoginEvents.SCAN, QRCodeLoginEvents.CONF):
                    await on_event("scanned", "")
                elif ev == QRCodeLoginEvents.DONE:
                    credential = getattr(result, "credential", None)
                    if credential is None:
                        await on_event("refuse", "登录结果缺少凭证")
                        return
                    data = credential.model_dump(by_alias=True)
                    self._cred_cache = (time.monotonic(), credential)
                    # QQ/微信/MOBILE 三条扫码授权路径库都不回写 client.credential（仅
                    # 手机验证码 phone_authorize 会）；不显式回写，换号重扫后取流仍走旧账号
                    if self._client is not None:
                        self._client.credential = credential
                    await on_event("done", json.dumps(data, ensure_ascii=False))
                    return
                elif ev == QRCodeLoginEvents.TIMEOUT:
                    await on_event("timeout", "")
                    return
                elif ev == QRCodeLoginEvents.REFUSE:
                    await on_event("refuse", "")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - 事件流异常（超时/网络）
            msg = str(e)
            await on_event("timeout" if ("超时" in msg or "timeout" in msg.lower()) else "refuse", msg[:120])

    # ──────────── 异常映射 ────────────
    @staticmethod
    def _map(e: BaseApiException) -> ApiError:
        """按异常类型映射，不靠匹配 message 字符串。

        库各异常的默认文案并不统一（CredentialExpiredError 是"登录凭证已过期, 请重新登录"，
        不含任何可匹配的中文关键词），按字符串匹配会让过期凭证走兜底分支、
        把英文原文透给用户。
        """
        if isinstance(e, CredentialExpiredError):
            return ApiError("QQ 登录凭证已过期，请重新扫码登录", code=getattr(e, "code", None), source="qq")
        if isinstance(e, RatelimitedError):
            hint = str(getattr(e, "feedback_url", "") or "")
            suffix = f"（{hint}）" if hint else ""
            return ApiError(
                f"QQ 音乐触发风控，请稍后再试{suffix}", code=getattr(e, "code", None), source="qq"
            )
        if isinstance(e, NetworkError):
            return ApiError("QQ 音乐网络异常，请稍后重试", code=getattr(e, "code", None), source="qq")
        msg = str(e)
        if "需要登录" in msg or "未提供有效" in msg:
            return ApiError("需要登录 QQ 音乐账号", code=getattr(e, "code", None), source="qq")
        if "风控" in msg:
            return ApiError("触发风控，请稍后再试", code=getattr(e, "code", None), source="qq")
        return ApiError(msg, code=getattr(e, "code", None), source="qq")


if available():  # 依赖缺失时不注册：create() 不会拿到一个必然失败的实例
    register("qq", lambda config, qq_device_path=None, **kw: QQClient(config, qq_device_path))
