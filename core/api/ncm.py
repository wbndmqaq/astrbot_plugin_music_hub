"""网易云音乐音源客户端（基于自建 api-enhanced HTTP 服务）。

所有端点 GET；cookie 以 query 传入（extra 覆盖全局同名键）；HTTP ≥400 与
body.code=301 都抛 ApiError。接口缓存 2 分钟，登录/轮询类请求自动拼 timestamp。
"""

from __future__ import annotations

import asyncio
import re
import time

import aiohttp

from ..errors import NCM_ERRORS, ApiError, NotEnabledError
from ..quality import NCM_LABEL, ladder_for
from .http import (
    API_TIMEOUT_SEC,
    collect,
    data_of,
    get_session,
    list_of,
    merge_cookie,
    num,
    opt_int,
    query_safe,
    safe_url,
)
from .registry import register

# 统一歌曲结构（本客户端产出）
# {source, sid, sid2, name, artist, album, cover, duration, dtMs,
#  pay, trial, mvid, fee, raw}

_VIP_CACHE_TTL = 300.0  # has_high_quality_privilege 的缓存秒数


def _err_msg(code, status: int = 0) -> str:
    c = opt_int(code)
    if c is not None and c in NCM_ERRORS:
        return NCM_ERRORS[c]
    if status >= 500:
        return f"网易云 API 服务错误（HTTP {status}）"
    if status >= 400:
        return f"请求失败（HTTP {status}）"
    return "请求失败"


def normalize_song(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name") or item.get("title") or ""
    if not name:
        return None
    album = item.get("album") or item.get("al") or {}
    if not isinstance(album, dict):
        album = {}
    arr = item.get("artists") or item.get("ar") or []
    artist = " / ".join(a.get("name") or "" for a in arr if isinstance(a, dict)).strip(" /")
    artist = artist or str(item.get("singer") or item.get("singers") or "")
    dt = num(item.get("dt") or item.get("duration"))
    fee = int(num(item.get("fee")))
    priv = item.get("privilege") if isinstance(item.get("privilege"), dict) else {}
    if priv.get("fee") is not None:
        fee = int(num(priv.get("fee")))
    mvid = item.get("mvid") or item.get("mv") or 0
    cover = album.get("picUrl") or ""
    if cover:
        cover += "?param=300y300"
    return {
        "index": idx + 1,
        "source": "ncm",
        "sid": str(item.get("id") or ""),
        "sid2": "",
        "name": str(name),
        "artist": artist,
        "album": album.get("name") or "",
        "cover": cover,
        "duration": _dur(dt),
        "dtMs": int(dt),
        "pay": fee in (1, 4),
        "trial": fee == 8,
        "mvid": str(opt_int(mvid) or ""),
        "fee": fee,
        "raw": {},
    }


def _dur(dt: float) -> str:
    if dt <= 0:
        return ""
    sec = int(dt / 1000)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def normalize_playlist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name") or ""
    if not name:
        return None
    creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
    return {
        "index": idx + 1,
        "id": str(item.get("id") or ""),
        "name": str(name),
        "cover": (item.get("picUrl") or item.get("coverImgUrl") or "") + "?param=300y300",
        "playCount": int(num(item.get("playCount"))),
        "trackCount": int(num(item.get("trackCount"))),
        "creator": creator.get("nickname") or "",
    }


def normalize_album(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name") or item.get("album") or ""
    if not name:
        return None
    artists = item.get("artists") or item.get("ar") or []
    artist = " / ".join(a.get("name") or "" for a in artists if isinstance(a, dict))
    return {
        "index": idx + 1,
        "id": str(item.get("id") or ""),
        "name": str(name),
        "cover": (item.get("picUrl") or item.get("pic") or "") + "?param=300y300",
        "artist": artist,
        "songCount": int(num(item.get("size") or item.get("songCount"))),
    }


def normalize_artist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "id": str(item.get("id") or ""),
        "name": str(name),
        "cover": (item.get("picUrl") or item.get("img1v1Url") or "") + "?param=300y300",
        "sub": "",
    }


class NeteaseClient:
    source = "ncm"

    def __init__(self, config):
        self._config = config  # core.config.Config
        self._vip_cache: tuple[float, bool] = (0.0, False)  # (检查时刻, 是否有特权)

    def ready(self) -> bool:
        """无重依赖（走 HTTP 服务），配置了地址即可用。"""
        return bool(self.base)

    # ──────────── 基础 ────────────
    @property
    def base(self) -> str:
        return self._config.src_api_base("ncm")

    @property
    def cookie(self) -> str:
        return self._config.src_cookie("ncm")

    def _require_base(self) -> str:
        base = self.base
        if not base:
            raise NotEnabledError("网易云 API 未配置", source="ncm")
        return base

    async def request(self, pathname: str, params: dict | None = None, *, with_cookie: bool = True) -> dict:
        from ..ratelimit import limiter

        base = self._require_base()
        await limiter.acquire("ncm")
        params = dict(params or {})
        if with_cookie:
            extra = params.pop("cookie", "")
            merged = merge_cookie(self.cookie, extra)
            if merged:
                params["cookie"] = merged
        url = f"{base}{pathname if pathname.startswith('/') else '/' + pathname}"
        timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SEC)
        try:
            async with get_session().get(url, params=query_safe(params), timeout=timeout) as res:
                return await self._handle(res, pathname)
        except aiohttp.ClientConnectorError as e:
            raise ApiError(f"无法连接网易云 API（{safe_url(base)}），请确认服务已启动", source="ncm") from e
        except aiohttp.ServerTimeoutError as e:
            raise ApiError("请求超时", source="ncm", timeout=True) from e
        except aiohttp.ClientError as e:
            raise ApiError(f"网络错误（{type(e).__name__}）：{safe_url(url)}", source="ncm") from e
        except asyncio.TimeoutError as e:
            # ClientTimeout(total=...) 到期：3.10 抛 asyncio.TimeoutError，3.11 起与内建 TimeoutError 同一类型
            raise ApiError("请求超时", source="ncm", timeout=True) from e

    async def _handle(self, res: aiohttp.ClientResponse, pathname: str) -> dict:
        status = res.status
        try:
            body = await res.json(content_type=None)
        except Exception:
            text = (await res.text())[:200]
            raise ApiError(f"返回非 JSON（HTTP {status}）：{text}", source="ncm") from None
        if not isinstance(body, dict):
            raise ApiError(f"返回格式异常（HTTP {status}）", source="ncm")
        if status >= 400:
            code = body.get("code")
            msg = body.get("msg") or body.get("message") or ""
            raise ApiError(str(msg) or _err_msg(code, status), code=code, source="ncm", payload=body)
        if body.get("code") == 301 and not pathname.startswith("/login"):
            raise ApiError(NCM_ERRORS[301], code=301, source="ncm", payload=body)
        return body

    @staticmethod
    def _ts(params: dict) -> dict:
        """防 api-enhanced 2 分钟 URL 缓存。"""
        params["timestamp"] = int(time.time() * 1000)
        return params

    # ──────────── 搜索 ────────────
    async def search(self, keyword: str, limit: int = 10, type_: int = 1, page: int = 0) -> list[dict]:
        body = await self.request(
            "/cloudsearch", {"keywords": keyword, "type": type_, "limit": limit, "offset": page * limit}
        )
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        items = (
            result.get("songs")
            if type_ == 1
            else result.get("playlists")
            if type_ == 1000
            else result.get("albums")
            if type_ == 10
            else result.get("artists")
            if type_ == 100
            else []
        )
        if type_ == 1:
            return collect(items, normalize_song, limit)
        if type_ == 1000:
            return collect(items, normalize_playlist, limit)
        if type_ == 10:
            return collect(items, normalize_album, limit)
        return collect(items, normalize_artist, limit)

    async def suggest(self, keyword: str) -> list[dict]:
        # 不带 type=mobile 走 web 端（mobile 端只回 allMatch 关键词，拿不到分类）
        body = await self.request("/search/suggest", {"keywords": keyword})
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        out = []
        seen = set()
        for key in ("artists", "albums", "songs"):
            for it in list_of(result.get(key)):
                name = it.get("name") or ""
                extra = ""
                if key == "songs":
                    arr = it.get("artists") or []
                    extra = " / ".join(a.get("name") or "" for a in arr if isinstance(a, dict))
                elif key == "albums":
                    # web 端专辑元素的歌手是单数字符串字段 artist
                    extra = str(it.get("artist") or "")
                if name and name not in seen:
                    seen.add(name)
                    out.append({"name": name, "sub": extra, "kind": key})
        return out[:10]

    async def hot_search(self) -> list[dict]:
        body = await self.request("/search/hot/detail")
        items = list_of(
            (body.get("data") or {}).get("list") if isinstance(body.get("data"), dict) else body.get("data")
        )
        return [
            {
                "index": i + 1,
                "word": it.get("searchWord") or it.get("word") or "",
                "hot": it.get("score") or it.get("hot") or "",
            }
            for i, it in enumerate(items)
            if isinstance(it, dict)
        ][:15]

    # ──────────── 取流 ────────────
    async def song_url(self, sid: str, level: str, *, unblock: bool = False) -> dict:
        # 必须拼 timestamp 穿透 api-enhanced 的 2 分钟 URL 缓存：网易云直链带时效签名，
        # 命中缓存会拿到已过期的 url，下载必失败，且 delivery 的重取重试同样会命中缓存
        params = self._ts({"id": sid, "level": level})
        if unblock:
            params["unblock"] = "true"
        if level == "dolby":
            params["cookie"] = "os=pc"
        body = await self.request("/song/url/v1", params)
        data = list_of(body.get("data"))
        d0 = data[0] if data and isinstance(data[0], dict) else {}
        return {
            "url": d0.get("url") or "",
            "level": d0.get("level") or level,
            "code": d0.get("code"),
            "fee": d0.get("fee"),
            "type": d0.get("type") or "",
            "raw": d0,
        }

    async def song_url_best(
        self, song: dict, preferred: str = "auto", *, unblock_fallback: bool = True
    ) -> dict:
        # 统一协议：与其他音源一致接收 song dict，内部取 sid
        sid = song.get("sid") or song.get("sid2") or ""
        if not sid:
            raise ApiError("歌曲缺少 id，无法取流", source="ncm")
        full = preferred in ("auto", "adaptive", "best") and await self.has_high_quality_privilege()
        levels = ladder_for("ncm", preferred, full=full)
        last_err: ApiError | None = None
        last_code = None
        for lv in levels:
            try:
                r = await self.song_url(sid, lv)
            except ApiError as e:
                if e.timeout:
                    raise
                last_err = e
                last_code = opt_int(e.code) or last_code
                continue
            if r.get("url"):
                r["quality"] = lv
                r["qualityLabel"] = NCM_LABEL.get(lv, lv)
                return r
            last_code = opt_int(r.get("code")) or last_code
            last_err = ApiError(f"无 {NCM_LABEL.get(lv, lv)} 音质", source="ncm")
        if unblock_fallback and self._config.src_quality_unblock("ncm"):
            try:
                r = await self.song_url(sid, "lossless", unblock=True)
                if r.get("url"):
                    r.update({"quality": "lossless", "qualityLabel": "解灰音源", "unblocked": True})
                    return r
                last_code = opt_int(r.get("code")) or last_code
            except ApiError as e:
                last_err = e
                last_code = opt_int(e.code) or last_code
        if last_code is not None and last_code in NCM_ERRORS:
            raise ApiError(NCM_ERRORS[last_code], code=last_code, source="ncm")
        if last_err is not None:
            raise last_err
        raise ApiError("无法获取播放链接（可能无版权或需要 VIP）", code=last_code, source="ncm")

    async def has_high_quality_privilege(self) -> bool:
        """auto 时是否从最高档起试：需登录且任一 VIP 未过期。

        结果按 5 分钟缓存——每次取流都调一次 /vip/info，连播/换源时纯属浪费；
        扫码登录换凭证后由 TTL 自然过期，无需手动失效。
        """
        if not self.cookie:
            return False
        now = time.monotonic()
        if now - self._vip_cache[0] < _VIP_CACHE_TTL:
            return self._vip_cache[1]
        privileged = await self._check_vip_privilege()
        self._vip_cache = (now, privileged)
        return privileged

    async def _check_vip_privilege(self) -> bool:
        try:
            body = await self.request("/vip/info")
            data = data_of(body)
            for key in ("redplus", "associator", "musicPackage"):
                node = data.get(key) if isinstance(data.get(key), dict) else {}
                if (
                    int(num(node.get("vipLevel"))) > 0
                    and int(num(node.get("expireTime"))) > time.time() * 1000
                ):
                    return True
        except ApiError:
            pass
        return False

    # ──────────── 歌曲 ────────────
    async def song_detail(self, sids: list[str]) -> list[dict]:
        body = await self.request("/song/detail", {"ids": ",".join(str(s) for s in sids)})
        return collect(body.get("songs"), normalize_song)

    async def lyric(self, sid: str) -> dict:
        body = await self.request("/lyric", {"id": sid})
        lrc = (body.get("lrc") or {}).get("lyric") if isinstance(body.get("lrc"), dict) else ""
        tly = (body.get("tlyric") or {}).get("lyric") if isinstance(body.get("tlyric"), dict) else ""
        return {"lrc": lrc or "", "tlyric": tly or "", "yrc": ""}

    async def lyric_yrc(self, sid: str) -> dict:
        """逐字歌词（/lyric/new yrc）；失败回退普通歌词。"""
        try:
            body = await self.request("/lyric/new", {"id": sid})
            yrc = (body.get("yrc") or {}).get("lyric") if isinstance(body.get("yrc"), dict) else ""
            lrc = (body.get("lrc") or {}).get("lyric") if isinstance(body.get("lrc"), dict) else ""
            tly = (body.get("tlyric") or {}).get("lyric") if isinstance(body.get("tlyric"), dict) else ""
            if yrc:
                return {"lrc": lrc or "", "tlyric": tly or "", "yrc": yrc}
        except ApiError:
            pass
        return await self.lyric(sid)

    # ──────────── song 协议 ────────────
    async def song_lyric(self, song: dict) -> dict:
        return await self.lyric(song["sid"])

    async def song_lyric_karaoke(self, song: dict) -> dict:
        """逐字歌词；lyric_yrc 内部已含失败回退普通歌词。"""
        return await self.lyric_yrc(song["sid"])

    async def song_comments(self, song: dict, limit: int = 12) -> dict:
        return await self.comments(song["sid"], limit)

    async def simi_songs(self, sid: str, limit: int = 10) -> list[dict]:
        body = await self.request("/simi/song", {"id": sid, "limit": limit})
        return collect(body.get("songs"), normalize_song, limit)

    async def simi_playlists(self, sid: str, limit: int = 10) -> list[dict]:
        """相似歌单：参数是**歌曲 id**（传歌单 id 不报错但恒空）。"""
        body = await self.request("/simi/playlist", {"id": sid, "limit": limit})
        return collect(body.get("playlists"), normalize_playlist, limit)

    async def related_playlists(self, pid: str, limit: int = 10) -> list[dict]:
        """相关歌单推荐：参数必须是歌单 id；上榜歌单没有推荐，返回空列表。"""
        body = await self.request("/playlist/detail/rcmd/get", {"id": pid})
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        out = []
        for it in list_of(data.get("recPlaylist")):
            pl = it.get("playlist") if isinstance(it, dict) and isinstance(it.get("playlist"), dict) else {}
            if pl.get("name"):
                out.append(pl)
        return collect(out, normalize_playlist, limit)

    # ──────────── 榜单 / 排行 ────────────
    async def rank_list(self) -> list[dict]:
        body = await self.request("/toplist")
        items = list_of(body.get("list"))
        return [
            {
                "index": i + 1,
                "id": str(it.get("id") or ""),
                "name": it.get("name") or "",
                "sub": f"{it.get('updateFrequency') or ''}",
                "cover": (it.get("coverImgUrl") or "") + "?param=300y300",
                "tag": "",
            }
            for i, it in enumerate(items)
            if isinstance(it, dict)
        ]

    async def rank_songs(self, rank_id: str, limit: int = 30) -> list[dict]:
        body = await self.request("/playlist/track/all", {"id": rank_id, "limit": limit, "offset": 0})
        songs = (body.get("songs") or []) if isinstance(body.get("songs"), list) else []
        privs = {p.get("id"): p for p in list_of(body.get("privileges")) if isinstance(p, dict)}
        merged = []
        for s in songs:
            if isinstance(s, dict) and s.get("id") in privs and isinstance(privs[s["id"]], dict):
                s = {**s, "privilege": privs[s["id"]]}
            merged.append(s)
        return collect(merged, normalize_song, limit)

    async def new_songs(self, area: int = 0) -> list[dict]:
        body = await self.request("/top/song", {"type": area})
        return collect(body.get("data"), normalize_song, 20)

    async def toplist_artist(self) -> list[dict]:
        body = await self.request("/toplist/artist", {"type": 1})
        node = body.get("list") if isinstance(body.get("list"), dict) else {}
        artists = list_of(node.get("artists"))
        out = []
        for i, it in enumerate(artists[:15]):
            if isinstance(it, dict) and it.get("name"):
                out.append(
                    {
                        "index": i + 1,
                        "id": str(it.get("id") or ""),
                        "name": it.get("name"),
                        "sub": f"热度 {it.get('score') or ''}".strip(),
                        "cover": (it.get("picUrl") or it.get("img1v1Url") or "") + "?param=300y300",
                    }
                )
        return out

    # ──────────── 歌单 / 专辑 / 歌手 ────────────
    async def playlist_detail(self, pid: str) -> dict:
        body = await self.request("/playlist/detail", {"id": pid})
        node = body.get("playlist") if isinstance(body.get("playlist"), dict) else {}
        return {
            "id": str(node.get("id") or pid),
            "name": node.get("name") or "",
            "cover": (node.get("coverImgUrl") or "") + "?param=500y500",
            "creator": (node.get("creator") or {}).get("nickname")
            if isinstance(node.get("creator"), dict)
            else "",
            "playCount": int(num(node.get("playCount"))),
            "trackCount": int(num(node.get("trackCount"))),
            "desc": str(node.get("description") or "")[:200],
        }

    async def playlist_songs(self, pid: str, limit: int = 30) -> list[dict]:
        body = await self.request("/playlist/track/all", {"id": pid, "limit": limit, "offset": 0})
        songs = list_of(body.get("songs"))
        return collect(songs, normalize_song, limit)

    async def playlist_songs_by_keyword(
        self, keyword: str, limit: int = 30
    ) -> tuple[dict | None, list[dict]]:
        """歌单搜索：唯一命中直接取曲目，多个返回候选列表。"""
        cands = await self.search(keyword, limit=10, type_=1000)
        if not cands:
            return None, []
        if len(cands) == 1:
            pl = await self.playlist_detail(cands[0]["id"])
            songs = await self.playlist_songs(cands[0]["id"], limit)
            return pl, songs
        return None, cands

    async def high_quality_playlists(self, cat: str = "") -> list[dict]:
        params = {"limit": 15}
        if cat:
            params["cat"] = cat
        body = await self.request("/top/playlist/highquality", params)
        return collect(body.get("playlists"), normalize_playlist, 15)

    async def playlist_categories(self) -> list[str]:
        body = await self.request("/playlist/catlist")
        out = []
        for cat in list_of(body.get("sub")):
            if isinstance(cat, dict) and cat.get("name"):
                out.append(str(cat["name"]))
        return out[:60]

    async def top_playlists(self, cat: str = "") -> list[dict]:
        params = {"limit": 15, "order": "hot"}
        if cat:
            params["cat"] = cat
        body = await self.request("/top/playlist", params)
        return collect(body.get("playlists"), normalize_playlist, 15)

    async def playlist_hot_tags(self) -> list[str]:
        body = await self.request("/playlist/hot")
        return [
            str(t.get("name")) for t in list_of(body.get("tags")) if isinstance(t, dict) and t.get("name")
        ][:20]

    async def album_detail(self, aid: str) -> dict:
        body = await self.request("/album", {"id": aid})
        node = body.get("album") if isinstance(body.get("album"), dict) else {}
        album = normalize_album(node)
        songs = collect(body.get("songs"), normalize_song)
        if album:
            album["desc"] = str(node.get("description") or "")[:200]
        return {"album": album, "songs": songs}

    async def album_newest(self) -> list[dict]:
        body = await self.request("/album/newest")
        return collect(body.get("albums"), normalize_album, 15)

    async def top_albums(self, area: str = "") -> list[dict]:
        """新碟榜：area 用 ZH/EA/KR/JP 原文（上游 /top/song 的数字 type 不通用）。"""
        params = {"limit": 15}
        if area in ("ZH", "EA", "KR", "JP"):
            params["area"] = area
        body = await self.request("/top/album", params)
        # 无 area 返回 weekData，带 area 返回 monthData
        return collect(body.get("monthData") or body.get("weekData"), normalize_album, 15)

    async def top_artists(self) -> list[dict]:
        body = await self.request("/top/artists", {"limit": 15})
        return collect(body.get("artists"), normalize_artist, 15)

    async def artist_top_songs(self, artist_id: str, limit: int = 30) -> list[dict]:
        body = await self.request("/artist/top/song", {"id": artist_id})
        return collect(body.get("songs"), normalize_song, limit)

    async def artist_albums(self, artist_id: str, limit: int = 15) -> list[dict]:
        body = await self.request("/artist/album", {"id": artist_id, "limit": limit})
        node = body.get("artist") if isinstance(body.get("artist"), dict) else {}
        return collect(node.get("albums") or body.get("hotAlbums"), normalize_album, limit)

    async def artist_songs_by_keyword(self, keyword: str, limit: int = 30) -> tuple[dict | None, list[dict]]:
        cands = await self.search(keyword, limit=5, type_=100)
        if not cands:
            return None, []
        artist = cands[0]
        songs = await self.artist_top_songs(artist["id"], limit)
        return artist, songs

    # ──────────── 推荐 / FM ────────────
    async def personalized(self, limit: int = 15) -> list[dict]:
        body = await self.request("/personalized", {"limit": limit})
        return collect(body.get("result"), normalize_playlist, limit)

    async def personalized_newsong(self) -> list[dict]:
        body = await self.request("/personalized/newsong")
        items = list_of(body.get("result"))
        songs = []
        for it in items:
            node = it.get("song") if isinstance(it.get("song"), dict) else it
            if isinstance(node, dict):
                songs.append(node)
        return collect(songs, normalize_song, 20)

    async def daily_recommend(self) -> list[dict]:
        body = await self.request("/recommend/songs")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return collect(data.get("dailySongs"), normalize_song, 30)

    async def personal_fm(self) -> list[dict]:
        body = await self.request("/personal_fm")
        # 实测 data 本身就是歌曲数组（不是 {song_list|songs} 字典）
        return collect(list_of(body.get("data")), normalize_song, 10)

    async def recommend_playlists(self) -> list[dict]:
        body = await self.request("/recommend/resource")
        return collect(body.get("recommend"), normalize_playlist, 15)

    async def banners(self) -> list[dict]:
        body = await self.request("/banner")
        return [
            {
                "index": i + 1,
                "name": it.get("typeTitle") or "",
                "sub": it.get("label") or "",
                "cover": (it.get("imageUrl") or it.get("bigImageUrl") or "") + "?param=400y160",
                "target": it.get("url") or "",
            }
            for i, it in enumerate(list_of(body.get("banners")))
            if isinstance(it, dict) and it.get("typeTitle")
        ][:10]

    async def top_mvs(self, limit: int = 10) -> list[dict]:
        body = await self.request("/top/mv", {"limit": limit})
        return [
            {
                "index": i + 1,
                "id": str(it.get("id") or ""),
                "name": it.get("name") or "",
                "artist": it.get("artistName") or "",
                "cover": (it.get("cover") or "") + "?param=300y180",
                "duration": _dur(num(it.get("duration"))),
                "playCount": int(num(it.get("playCount"))),
            }
            for i, it in enumerate(list_of(body.get("data")))
            if isinstance(it, dict) and it.get("name")
        ][:limit]

    async def dj_radios(self, limit: int = 10) -> list[dict]:
        body = await self.request("/dj/recommend")
        return [
            {
                "index": i + 1,
                "id": str(it.get("id") or ""),
                "name": it.get("name") or "",
                "sub": f"{it.get('category') or ''} · {int(num(it.get('programCount')))} 期".strip(" ·"),
                "cover": (it.get("picUrl") or "") + "?param=300y300",
                "playCount": int(num(it.get("playCount"))),
            }
            for i, it in enumerate(list_of(body.get("djRadios")))
            if isinstance(it, dict) and it.get("name")
        ][:limit]

    # ──────────── 评论 / MV ────────────
    async def comments(self, sid: str, limit: int = 12, kind: str = "music") -> dict:
        path = {"music": "/comment/music", "playlist": "/comment/playlist", "album": "/comment/album"}[kind]
        body = await self.request(path, {"id": sid, "limit": limit})
        hot = collect(body.get("hotComments"), self._norm_comment, limit)
        new = collect(body.get("comments"), self._norm_comment, limit)
        total = int(num(body.get("total")))
        return {"hot": hot, "new": new, "total": total}

    @staticmethod
    def _norm_comment(item: dict, idx: int = 0) -> dict | None:
        if not isinstance(item, dict):
            return None
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        content = re.sub(r"\[em\]e\d+\[/em\]", "", str(item.get("content") or ""))
        content = content.replace("\r", " ").strip()
        if not content:
            return None
        time_str = ""
        try:
            ts = num(item.get("time")) / 1000
            if ts > 0:
                time_str = time.strftime("%Y-%m-%d", time.localtime(ts))
        except Exception:
            pass
        return {
            "index": idx + 1,
            "nick": user.get("nickname") or "匿名",
            "avatar": (user.get("avatarUrl") or "") + "?param=80y80",
            "time": time_str,
            "likes": int(num(item.get("likedCount"))),
            "content": content[:300],
            "hot": bool(item.get("hotComment")),
        }

    async def mv_url(self, mvid: str, r: int = 1080) -> dict:
        body = await self.request("/mv/url", {"id": mvid, "r": r})
        node = data_of(body)
        return {
            "url": node.get("url") or "",
            "r": int(num(node.get("r")) or r),
            "size": int(num(node.get("size"))),
        }

    async def song_mv_url(self, song: dict) -> dict:
        """统一协议：取 MV 直链；无 mvid 返回空。"""
        mvid = song.get("mvid")
        if not mvid:
            return {"url": ""}
        return await self.mv_url(str(mvid))

    async def mv_search(self, keyword: str, limit: int = 8) -> list[dict]:
        body = await self.request("/cloudsearch", {"keywords": keyword, "type": 1004, "limit": limit})
        result = body.get("result") if isinstance(body.get("result"), dict) else {}
        out = []
        for i, mv in enumerate(list_of(result.get("mvs"))):
            if not isinstance(mv, dict) or not mv.get("name"):
                continue
            artists = mv.get("artists") or []
            out.append(
                {
                    "index": i + 1,
                    "id": str(mv.get("id") or ""),
                    "name": mv.get("name"),
                    "artist": " / ".join(a.get("name") or "" for a in artists if isinstance(a, dict)),
                    "cover": (mv.get("cover") or "") + "?param=300y180",
                    "duration": _dur(num(mv.get("duration"))),
                    "playCount": int(num(mv.get("playCount"))),
                }
            )
        return out

    # ---- 用户（需登录）----
    async def login_status(self) -> dict:
        body = await self.request("/login/status")
        node = data_of(body)
        profile = node.get("profile") if isinstance(node.get("profile"), dict) else {}
        uid = str(profile.get("userId") or node.get("userId") or "")
        return {
            "loggedIn": bool(profile.get("userId")),
            "uid": uid,
            "nickname": profile.get("nickname") or "",
            "avatar": (profile.get("avatarUrl") or "") + "?param=100y100",
        }

    async def vip_info(self) -> dict:
        body = await self.request("/vip/info")
        data = data_of(body)
        red = data.get("redVipLevel") if isinstance(data.get("redVipLevel"), (int, float)) else 0
        return {"vipLevel": int(num(red)), "vipType": int(num(data.get("redVipType")))}

    async def like_list(self, uid: str) -> list[str]:
        body = await self.request("/likelist", {"uid": uid})
        ids = body.get("ids") or []
        return [str(i) for i in ids if i]

    async def user_record(self, uid: str) -> list[dict]:
        body = await self.request("/user/record", {"uid": uid, "type": 1})
        week = body.get("weekData") if isinstance(body.get("weekData"), list) else []
        out = []
        for i, it in enumerate(week[:20]):
            if not isinstance(it, dict):
                continue
            song = it.get("song") if isinstance(it.get("song"), dict) else {}
            norm = normalize_song(song, i)
            if norm:
                norm["sub"] = f"听了 {int(num(it.get('playCount')))} 次"
                out.append(norm)
        return out

    async def daily_signin(self) -> str:
        try:
            body = await self.request("/daily_signin", {"type": 0})
            # 响应是嵌套结构 {android:{code}, web:{code}}（module 注释），顶层 code 兼容
            code = opt_int(body.get("code"))
            if code is None:
                node = body.get("android") if isinstance(body.get("android"), dict) else {}
                code = opt_int(node.get("code"))
            if code == 200:
                return "签到成功"
            if code == -2:
                return "今天已经签到过啦"
            return f"签到返回 {code}"
        except ApiError as e:
            # 重复签到上游以 HTTP 400 + code=-2 返回
            if opt_int(e.code) == -2:
                return "今天已经签到过啦"
            return e.user_msg()

    async def user_cloud(self, limit: int = 30) -> list[dict]:
        body = await self.request("/user/cloud", {"limit": limit})
        items = list_of((body.get("data") or {}).get("list") if isinstance(body.get("data"), dict) else [])
        out = []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            out.append(
                {
                    "index": i + 1,
                    "sid": str(it.get("songId") or ""),
                    "name": it.get("songName") or it.get("fileName") or "",
                    "artist": it.get("artist") or "",
                    "album": it.get("album") or "",
                    "size": f"{int(num(it.get('fileSize')) / 1024 / 1024):.1f}MB",
                }
            )
        return out

    async def recent_songs(self, limit: int = 30) -> list[dict]:
        body = await self.request("/record/recent/song", {"limit": 100})
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        items = list_of(data.get("list"))
        out = []
        for it in items:
            node = it.get("data") if isinstance(it.get("data"), dict) else {}
            if not node:
                continue
            res = node.get("resourceList") if isinstance(node.get("resourceList"), dict) else {}
            song = res.get("songInfo") if isinstance(res.get("songInfo"), dict) else {}
            norm = normalize_song(song)
            if norm:
                ts = num(it.get("playTime")) / 1000
                norm["sub"] = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts > 0 else ""
                out.append(norm)
        return out[:limit]

    async def user_playlists(self, uid: str) -> list[dict]:
        body = await self.request("/user/playlist", {"uid": uid})
        return collect(body.get("playlist"), normalize_playlist, 30)

    async def like_song(self, sid: str, like: bool) -> str:
        # 红心是写操作，api-enhanced 对所有 200 响应缓存 2 分钟，必须带 timestamp 穿透
        check = await self.request("/song/like/check", self._ts({"ids": f"[{sid}]"}))
        already = sid in [str(i) for i in list_of(check.get("ids"))]
        if like and already:
            return "这首歌已经在红心列表里啦"
        body = await self.request("/like", self._ts({"id": sid, "like": "true" if like else "false"}))
        if opt_int(body.get("code")) == 200:
            return "已加入红心 ❤" if like else "已取消红心"
        raise ApiError("红心操作失败", source="ncm")

    async def history_recommend(self) -> list[dict]:
        body = await self.request("/history/recommend/songs")
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        songs = list_of(data.get("songs"))
        # 不走 /history/recommend/songs/detail 兜底：该接口 date 必选且服务端无 dates
        # 列表路由可查，盲传日期只会报错（主接口已登录即有数据）
        return collect(songs, normalize_song, 30)

    # ──────────── 登录 ────────────
    async def qr_key(self) -> str:
        body = await self.request("/login/qr/key", self._ts({}))
        key = (body.get("data") or {}).get("unikey") if isinstance(body.get("data"), dict) else ""
        if not key:
            raise ApiError("获取二维码 Key 失败", source="ncm")
        return str(key)

    async def qr_create(self, key: str) -> dict:
        body = await self.request("/login/qr/create", self._ts({"key": key, "qrimg": "true"}))
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {"qrurl": data.get("qrurl") or "", "qrimg": data.get("qrimg") or ""}

    async def qr_check(self, key: str) -> dict:
        """800 过期 / 801 待扫 / 802 已扫待确认 / 803 成功（带 cookie）。"""
        body = await self.request("/login/qr/check", self._ts({"key": key, "noCookie": "true"}))
        code = opt_int(body.get("code"))
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        out = {
            "code": code,
            "cookie": "",
            "msg": {800: "二维码已过期", 801: "等待扫码", 802: "已扫码，请在手机上确认", 803: "登录成功"}.get(
                code, f"状态 {code}"
            ),
        }
        if code == 803:
            out["cookie"] = body.get("cookie") or data.get("cookie") or ""
        return out

    async def logout(self) -> None:
        try:
            await self.request("/logout", self._ts({}))
        except ApiError:
            pass


register("ncm", lambda config, **kw: NeteaseClient(config))
