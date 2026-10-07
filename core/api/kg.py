"""酷狗音乐音源客户端（基于自建 KuGouMusicApi HTTP 服务）。

- 失败响应统一 HTTP 502 + error_code；业务成功看 body.status==1，status==2 是
  「该音质不可用」的降级信号
- 设备 dfid 通过 /register/dev 注册并持久化（data/plugin_data/<plugin>/device_cookies.json）
- 酷狗每个音质一个专属 hash，取链按档位走对应 hash（见 quality.kg_hash_for）
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..errors import KG_ERRORS, ApiError, NotEnabledError
from ..quality import KG_LABEL, first, kg_hash_for, ladder_for
from .http import (
    collect,
    data_of,
    format_duration,
    get_session,
    list_of,
    num,
    opt_int,
    request_json,
    with_ts,
)
from .registry import register


def _upstream_msg(data: dict) -> str:
    # 上游网络异常时 msg 可能是被 JSON 序列化的 Error 对象（"{}"），一并排除
    for key in ("msg", "error_msg", "errmsg", "message", "info"):
        v = data.get(key)
        if isinstance(v, str) and v.strip() and v.strip() != "{}":
            return v
    return ""


def normalize_song(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    base = item.get("base") if isinstance(item.get("base"), dict) else {}
    ainfo = item.get("audio_info") if isinstance(item.get("audio_info"), dict) else {}
    name = str(
        base.get("songname")
        or base.get("audio_name")
        or item.get("OriSongName")
        or item.get("official_songname")
        or item.get("songname")
        or item.get("SongName")
        or item.get("audio_name")
        or item.get("name")
        or ""
    ).strip()
    if not name:
        # FileName 兜底：上游两种大小写都出现过（乐库 vip 位是小写 filename）
        fn = str(item.get("FileName") or item.get("filename") or "").strip()
        if " - " in fn:
            name = fn.partition(" - ")[2].strip()
    if not name:
        return None
    artist = _singers(base) or _singers(item)
    if not artist and " - " in str(item.get("FileName") or item.get("filename") or ""):
        artist = str(item.get("FileName") or item.get("filename")).partition(" - ")[0].strip()
    cover = _cover_of(item)
    if not cover:
        alinfo = item.get("album_info") if isinstance(item.get("album_info"), dict) else {}
        cover = str(alinfo.get("cover") or "").replace("{size}", "300")
    h128 = str(ainfo.get("hash") or item.get("hash_128") or item.get("hash") or item.get("FileHash") or "")
    timelength = ainfo.get("timelength") or item.get("timelength") or item.get("time_length") or 0
    # timelength 单位可静态确定为毫秒（见 _dur），显式传 "ms" 修复 <10s 音频被格式化成
    # 150:00 的问题；Duration/duration 单位未知，退回 "auto" 猜测
    return {
        "index": idx + 1,
        "source": "kg",
        "sid": h128,
        "sid2": _mix_id(base) or _mix_id(item),
        "name": name,
        "artist": artist,
        "album": str(base.get("album_name") or item.get("AlbumName") or item.get("album_name") or ""),
        "cover": cover,
        "duration": _dur(num(timelength), "ms") or _dur(num(item.get("Duration") or item.get("duration"))),
        "dtMs": int(num(timelength)),
        "pay": _paid(item),
        "trial": False,
        "mvid": "",
        "hash_128": h128,
        "hash_320": str(ainfo.get("hash_320") or item.get("hash_320") or _sub(item, "HQ").get("Hash") or ""),
        "hash_flac": str(
            ainfo.get("hash_flac") or item.get("hash_flac") or _sub(item, "SQ").get("Hash") or ""
        ),
        "hash_high": str(
            ainfo.get("hash_high") or item.get("hash_high") or _sub(item, "Res").get("Hash") or ""
        ),
        "raw": {},
    }


def _sub(item: dict, key: str) -> dict:
    v = item.get(key)
    return v if isinstance(v, dict) else {}


def _singers(item: dict) -> str:
    for key in ("Singers", "singerinfo", "authors", "singers"):
        arr = item.get(key)
        if isinstance(arr, list):
            names = [
                str(a.get("name") or a.get("author_name") or "").strip() for a in arr if isinstance(a, dict)
            ]
            names = [n for n in names if n]
            if names:
                return " / ".join(names)
    for key in ("SingerName", "author_name", "singername", "singer"):
        v = item.get(key)
        if v:
            return str(v)
    return ""


def _cover_of(item: dict) -> str:
    tp = item.get("trans_param") if isinstance(item.get("trans_param"), dict) else {}
    c = first(
        tp.get("union_cover"),
        item.get("Image"),
        item.get("AlbumImage"),
        item.get("sizable_cover"),
        item.get("img"),
        item.get("cover"),
    )
    return c.replace("{size}", "300")


def _mix_id(item: dict) -> str:
    for key in ("MixSongID", "mixsongid", "album_audio_id", "Audioid", "audio_id", "songid"):
        v = item.get(key)
        if v is not None and str(v) not in ("", "0"):
            return str(v)
    return ""


def _paid(item: dict) -> bool:
    pay = item.get("PayType")
    if pay is None:
        pay = item.get("pay_type")
    if pay is not None:
        try:
            return int(pay) > 0
        except (TypeError, ValueError):
            pass
    fp = item.get("fail_process")
    if fp is not None:
        try:
            return int(fp) != 0
        except (TypeError, ValueError):
            return bool(fp)
    return False


def _dur(value: float, unit: str = "auto") -> str:
    """时长 → mm:ss。unit="ms" 恒按毫秒；"auto" 是兼容未知单位的阈值猜测：≥10000
    视为毫秒、否则视为秒。猜测的边界（已知局限）：小于 10s 的音频（如 9000ms 试听）
    会被当成 9000 秒格式化成 150:00；超过 10000s（约 2h47m）的秒值会被当成毫秒。
    单位依据：timelength 系字段恒为毫秒（上游 audio_match 页 formatDuration 按 ms
    处理、插件 dtMs 亦按 ms 消费），可显式传 "ms"；搜索结果的 Duration/duration
    系字段单位在上游代理源码中无从核实，只能保留 "auto"。"""
    if value <= 0:
        return ""
    if unit == "ms" or (unit == "auto" and value >= 10000):
        value /= 1000
    sec = int(value)
    return f"{sec // 60:02d}:{sec % 60:02d}"


def normalize_playlist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("specialname") or item.get("name") or item.get("special_name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "id": str(
            item.get("global_collection_id")
            or item.get("specialid")
            or item.get("id")
            or item.get("listid")
            or ""
        ),
        "name": str(name),
        "cover": _cover_of(item) or str(item.get("pic") or item.get("img") or "").replace("{size}", "300"),
        "trackCount": int(
            num(item.get("count") or item.get("m_count") or item.get("song_count") or item.get("songcount"))
        ),
        "playCount": int(num(item.get("play_count") or item.get("playnum") or item.get("listen_num"))),
        "creator": str(
            item.get("list_create_username")
            or item.get("nickname")
            or item.get("author_name")
            or item.get("user_name")
            or ""
        ),
    }


def normalize_album(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("albumname") or item.get("album_name") or item.get("name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "id": str(item.get("albumid") or item.get("album_id") or item.get("id") or ""),
        "name": str(name),
        "cover": _cover_of(item) or str(item.get("img") or "").replace("{size}", "300"),
        "artist": _singers(item) or str(item.get("singer") or ""),
        "songCount": int(num(item.get("songcount") or item.get("song_count"))),
    }


def normalize_artist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("AuthorName") or item.get("author_name") or item.get("name") or ""
    if not name:
        return None
    dy = item.get("dycover")
    if isinstance(dy, dict):
        cover = str(dy.get("first_frame_image") or item.get("avatar") or "")
    else:
        cover = str(dy or item.get("Avatar") or item.get("avatar") or "")
    return {
        "index": idx + 1,
        "id": str(
            item.get("AuthorId") or item.get("author_id") or item.get("singerid") or item.get("id") or ""
        ),
        "name": str(name),
        "cover": cover.replace("{size}", "300"),
        "sub": "",
    }


class KugouClient:
    source = "kg"

    def __init__(self, config, device_path: Path):
        self._config = config
        self._device_path = device_path  # device_cookies.json
        self._device_cookie = ""
        self._device_lock = asyncio.Lock()  # 并发首调只放一个进 /register/dev

    def ready(self) -> bool:
        """无重依赖（走 HTTP 服务），配置了地址即可用。"""
        return bool(self.base)

    # ──────────── 设备 ────────────
    def load_device(self) -> None:
        try:
            data = json.loads(self._device_path.read_text("utf-8"))
            self._device_cookie = str(data.get("cookie") or "")
        except (OSError, ValueError):
            self._device_cookie = ""

    def _jar_cookie(self) -> str:
        """注册响应后从共享会话 cookie jar 取该域名下的完整设备 cookie 串。

        KuGouMusicApi 服务端会把 dfid + KUGOU_API_GUID/MID/DEV… 经 Set-Cookie 回写，
        request.js 签名依赖这些键 —— 必须整串持久化，重启后不因缺 MID 触发 20028。
        """
        try:
            from yarl import URL

            jar = get_session().cookie_jar
            cookies = jar.filter_cookies(URL(self.base))
            parts = [f"{k}={m.value}" for k, m in cookies.items()]
            return "; ".join(p for p in parts if p)
        except Exception:  # noqa: BLE001 - jar 读取失败退回仅 dfid 的旧行为
            return ""

    async def ensure_device(self) -> str:
        """设备 cookie（dfid=... + KUGOU_API_*）。注册接口限频且静默失败：连发只有
        第一次返回 dfid，其余返回空 data —— 必须判断 dfid 存在并复用缓存。

        磁盘读写走 to_thread：本方法被大量接口调用，裸同步 IO 会卡住整个事件循环。
        """
        if self._device_cookie:
            return self._device_cookie
        async with self._device_lock:
            if self._device_cookie:  # 双检：等锁期间可能已被并发调用方填好
                return self._device_cookie
            await asyncio.to_thread(self.load_device)
            if self._device_cookie:
                return self._device_cookie
            body = await self.request("/register/dev", {}, inject_cookie=False)
            dfid = str(data_of(body).get("dfid") or "")
            cookie = self._jar_cookie()
            if dfid and "dfid=" not in cookie:
                cookie = f"{cookie}; " if cookie else ""
                cookie += f"dfid={dfid}"
            # 上游对每个请求都 Set-Cookie（KUGOU_API_*），jar 里必有非空 cookie；
            # 注册被静默限频时 dfid 为空。缺 dfid 的设备串一旦持久化，之后每次取流
            # 都被服务端注入随机 dfid（等于每次换新设备），放大 20028 风控 —— 宁可
            # 抛错让调用方稍后重试，也不落盘坏缓存。
            if not cookie or ("dfid=" not in cookie and not dfid):
                raise ApiError("设备注册失败：未拿到 dfid（上游静默限频），稍后重试", source="kg")
            await asyncio.to_thread(self._persist_device, cookie, dfid)
            self._device_cookie = cookie
            return cookie

    def _persist_device(self, cookie: str, dfid: str) -> None:
        """落盘设备 cookie（同步，供 to_thread 调用）。"""
        try:
            self._device_path.parent.mkdir(parents=True, exist_ok=True)
            self._device_path.write_text(
                json.dumps({"cookie": cookie, "dfid": dfid, "ts": int(time.time())}, ensure_ascii=False),
                "utf-8",
            )
        except OSError:
            pass

    # ──────────── 基础 ────────────
    @property
    def base(self) -> str:
        return self._config.src_api_base("kg")

    @property
    def cookie(self) -> str:
        return self._config.src_cookie("kg")

    def _require_base(self) -> str:
        base = self.base
        if not base:
            raise NotEnabledError("酷狗 API 未配置", source="kg")
        return base

    def _compose_cookie(self, caller: str) -> str:
        """设备 cookie + 登录 cookie + 调用方补充。匿名请求不带占位 cookie（已失效）。"""
        parts = []
        if self._device_cookie:
            parts.append(self._device_cookie)
        if self.cookie:
            parts.append(self.cookie)
        if caller:
            parts.append(caller)
        return "; ".join(p for p in parts if p)

    async def request(
        self,
        pathname: str,
        params: dict | None = None,
        method: str = "get",
        *,
        inject_cookie: bool = True,
    ) -> dict:
        base = self._require_base()
        params = dict(params or {})
        if inject_cookie:
            caller = str(params.pop("cookie", "") or "")
            composed = self._compose_cookie(caller)
            if composed:
                params["cookie"] = composed
        status, body = await request_json("kg", "酷狗 API", base, pathname, params, method=method)
        return self._handle(body, status)

    def _handle(self, body: dict, status: int) -> dict:
        """业务级错误映射。传输/解析层（网络异常、非 JSON）在 http.request_json；
        status==0 是 KuGouMusicApi 的上游故障约定（HTTP 可能仍是 502/200）。"""
        if status >= 400 or body.get("status") == 0:
            code = body.get("error_code") or body.get("errcode") or body.get("err_code")
            msg = _upstream_msg(body)
            c = opt_int(code)
            err_msg = str(msg) or (KG_ERRORS.get(c, "") if c else "") or f"酷狗 API 错误（HTTP {status}）"
            raise ApiError(err_msg, code=c, source="kg", payload=body)
        return body

    # 实现收敛到 http（with_ts / format_duration）；保留旧名以维持内部调用点与
    # tests/test_v3_api 对 kg._dur 的引用不漂移
    _ts = staticmethod(with_ts)
    _dur = staticmethod(format_duration)

    # ──────────── 搜索 ────────────
    def require_login(self) -> None:
        """酷狗已禁止匿名搜索（error_code=152）：占位 cookie 与设备 cookie 均无效。

        未登录时提前拦截，避免发出注定失败的请求，也让上层能给出扫码引导。
        """
        if not self.cookie:
            # 不带 code：错误码表里 20010 是泛化文案，会盖掉这里的扫码引导（errors.user_msg 优先查表）
            raise ApiError("酷狗搜索需登录，请先发送「kg登录」扫码", source="kg")

    async def search(self, keyword: str, limit: int = 10, type_: str = "song", page: int = 1) -> list[dict]:
        """page 从 1 起（ncm 是 0 起）——三源 page 语义不统一，调用方传值时注意。"""
        self.require_login()
        await self.ensure_device()
        body = await self.request(
            "/search", {"keywords": keyword, "type": type_, "pagesize": limit, "page": page}
        )
        lists = data_of(body).get("lists")
        if type_ == "song":
            return collect(lists, normalize_song, limit)
        if type_ == "special":
            return collect(lists, normalize_playlist, limit)
        if type_ == "album":
            return collect(lists, normalize_album, limit)
        # 酷狗搜索类型白名单是 special/lyric/song/album/author/mv：歌手搜索的 type 是 author
        if type_ == "author":
            return collect(lists, normalize_artist, limit)
        return []

    async def suggest(self, keyword: str) -> list[dict]:
        body = await self.request("/search/suggest", self._ts({"keywords": keyword}))
        data = data_of(body)
        out = []
        for spec in (("MusicTip", "song"), ("AlbumTip", "album"), ("AuthorTip", "author")):
            for it in list_of(data.get(spec[0])):
                if isinstance(it, dict) and (it.get("name") or it.get("keyword")):
                    out.append(
                        {
                            "name": str(it.get("name") or it.get("keyword")),
                            "sub": str(it.get("author_name") or ""),
                            "kind": spec[1],
                        }
                    )
        return out[:10]

    async def hot_search(self) -> list[dict]:
        body = await self.request("/search/hot")
        items = list_of(data_of(body).get("list"))
        return [
            {"index": i + 1, "word": it.get("keyword") or it.get("word") or it.get("name") or "", "hot": ""}
            for i, it in enumerate(items)
            if isinstance(it, dict)
        ][:15]

    # ──────────── 取流 ────────────
    async def song_url(self, hash_: str, quality: str, *, free_part: bool = False) -> dict:
        await self.ensure_device()
        body = await self.request(
            "/song/url", self._ts({"hash": hash_, "quality": quality, "free_part": 1 if free_part else 0})
        )
        body = body if isinstance(body, dict) else {}
        url = ""
        for key in ("url", "backupUrl"):
            v = body.get(key)
            if isinstance(v, list):
                url = next((str(u) for u in v if u), "")
            elif v:
                url = str(v)
            if url:
                break
        return {"url": url, "status": body.get("status"), "extName": body.get("extName") or "", "raw": body}

    @staticmethod
    def _is_trial_url(url: str) -> bool:
        u = (url or "").lower()
        return "p_0_" in u and "full" not in u

    async def song_url_best(
        self, song: dict, preferred: str = "auto", *, trial_fallback: bool = True
    ) -> dict:
        ladder = ladder_for("kg", preferred)
        last_err: ApiError | None = None
        last_paid = False
        for q in ladder:
            h = kg_hash_for(song, q)
            if not h:
                continue
            try:
                r = await self.song_url(h, q)
            except ApiError as e:
                if e.timeout:
                    raise
                last_err = e
                continue
            if r.get("url") and r.get("status") == 1:
                trial = self._is_trial_url(r["url"])
                return {
                    "url": r["url"],
                    "quality": "128" if trial else q,
                    "qualityLabel": KG_LABEL.get("128", "") if trial else KG_LABEL.get(q, q),
                    "trial": trial,
                    "extName": r.get("extName") or "",
                    "raw": r.get("raw"),
                }
            if r.get("status") == 2:
                last_paid = True
        if trial_fallback and self._config.src_quality_unblock("kg"):
            h128 = kg_hash_for(song, "128")
            if h128:
                try:
                    r = await self.song_url(h128, "128", free_part=True)
                except ApiError as e:
                    if e.timeout:
                        raise
                    last_err = e
                else:
                    if r.get("url") and r.get("status") == 1:
                        return {
                            "url": r["url"],
                            "quality": "128",
                            "qualityLabel": "试听 60s",
                            "trial": True,
                            "extName": r.get("extName") or "",
                            "raw": r.get("raw"),
                        }
        if last_err is not None and opt_int(last_err.code) in (20010, 20017, 20028, 20040):
            raise last_err
        if last_paid:
            # 不带 code=20017：那在 KG_ERRORS 表里会被 user_msg() 覆盖成
            # 「需要登录或 Token 失效」，这里语义是付费/VIP，落原始 message
            raise ApiError("该歌曲为付费/VIP，未登录无法获取完整播放链接", source="kg")
        if last_err is not None:
            raise last_err
        raise ApiError("无法获取播放链接（可能已下架或无版权）", source="kg")

    async def audio_by_hash(self, hash_: str) -> dict | None:
        await self.ensure_device()
        body = await self.request("/audio", {"hash": hash_})
        data = list_of(body.get("data"))
        item = data[0] if data and isinstance(data[0], dict) else {}
        if not item or not (item.get("hash") or item.get("audio_id")):
            return None
        return normalize_song(item)

    # ──────────── 歌词 ────────────
    async def lyric_candidates(
        self, *, hash_: str = "", keywords: str = "", album_audio_id: str = ""
    ) -> list[dict]:
        await self.ensure_device()
        body = await self.request(
            "/search/lyric", {"hash": hash_, "keywords": keywords, "album_audio_id": album_audio_id}
        )
        return [
            {
                "index": i + 1,
                "id": c.get("id") or c.get("download_id") or "",
                "accesskey": c.get("accesskey") or "",
                "song": c.get("song") or "",
                "singer": c.get("singer") or "",
                "duration": _dur(num(c.get("duration"))),
            }
            for i, c in enumerate(list_of(body.get("candidates")))
            if isinstance(c, dict)
        ]

    async def lyric(self, cid: str, accesskey: str, fmt: str = "lrc") -> str:
        await self.ensure_device()
        body = await self.request("/lyric", {"id": cid, "accesskey": accesskey, "fmt": fmt, "decode": 1})
        return str(body.get("decodeContent") or body.get("content") or "")

    # ──────────── song 协议（service 零分支委托的统一入口） ────────────
    async def _locate_candidates(self, song: dict) -> list[dict]:
        """按歌曲 hash + 歌名歌手 + album_audio_id 三重线索定位歌词候选。"""
        return await self.lyric_candidates(
            hash_=song.get("sid", ""),
            keywords=f"{song.get('name', '')} {song.get('artist', '')}".strip(),
            album_audio_id=song.get("sid2", ""),
        )

    async def song_lyric(self, song: dict) -> dict:
        """song 协议：普通歌词（LRC）。候选定位 → 取内容两步在源内闭环。"""
        for c in await self._locate_candidates(song):
            if c.get("id") and c.get("accesskey"):
                content = await self.lyric(c["id"], c["accesskey"], "lrc")
                return {"lrc": content, "tlyric": "", "yrc": ""}
        return {"lrc": "", "tlyric": "", "yrc": ""}

    async def song_lyric_karaoke(self, song: dict) -> dict:
        """song 协议：逐字歌词（KRC）。无候选或全无 accesskey 时返回空 yrc。"""
        for c in await self._locate_candidates(song):
            if c.get("id") and c.get("accesskey"):
                content = await self.lyric(c["id"], c["accesskey"], "krc")
                if content:
                    return {"lrc": "", "tlyric": "", "yrc": content}
        return {"lrc": "", "tlyric": "", "yrc": ""}

    async def song_comments(self, song: dict, limit: int = 12) -> dict:
        """song 协议：歌曲评论。/comment/music 只认 mixsongid（album_audio_id），
        缺 sid2 时返回空结果，不拿 hash 硬凑参数（语义不符只会静默取不到评论）。"""
        sid2 = song.get("sid2", "")
        if not sid2:
            return {"hot": [], "new": [], "total": 0}
        return await self.comments(sid2, limit)

    # ──────────── 歌曲 / 增强 ────────────
    async def song_climax(self, hash_: str) -> dict:
        await self.ensure_device()
        body = await self.request("/song/climax", {"hash": hash_})
        data = body.get("data")
        d0 = None
        if isinstance(data, list) and data:
            d0 = data[0]
        elif isinstance(data, dict):
            d0 = data.get(hash_) or data.get(str(hash_).lower()) or data
        if not isinstance(d0, dict):
            return {}
        return {"start_ms": int(num(d0.get("start_time"))), "end_ms": int(num(d0.get("end_time")))}

    async def ai_recommend(self, mixsongid: str, pagesize: int = 20) -> list[dict]:
        await self.ensure_device()
        # 上游 module 不读 pagesize（返回条数不可控），只用于本地截断
        body = await self.request("/ai/recommend", {"album_audio_id": mixsongid})
        return collect(data_of(body).get("song_list"), normalize_song, pagesize)

    async def related_songs(self, album_audio_id: str) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/audio/related", {"album_audio_id": album_audio_id})
        return collect(list_of(body.get("data")), normalize_song, 20)

    # ──────────── 榜单 ────────────
    async def rank_list(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/rank/list", self._ts({}))
        items = list_of(data_of(body).get("info"))
        return [
            {
                "index": i + 1,
                "id": str(it.get("rankid") or it.get("id") or ""),
                "name": it.get("rankname") or it.get("name") or "",
                "sub": it.get("update_frequency") or it.get("intro") or "",
                "cover": str(it.get("imgurl") or it.get("img") or "").replace("{size}", "300"),
                "tag": "",
            }
            for i, it in enumerate(items)
            if isinstance(it, dict)
        ][:30]

    async def rank_songs(self, rank_id: str, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/rank/audio", self._ts({"rankid": rank_id, "pagesize": limit, "page": 1}))
        songs = list_of(data_of(body).get("songs"))
        if not songs:
            songs = list_of(body.get("data"))
        return collect(songs, normalize_song, limit)

    async def new_songs(self, rank_id: int = 21608) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/top/song", {"type": rank_id})
        return collect(list_of(body.get("data")), normalize_song, 20)

    async def top_playlists(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/top/playlist", {"pagesize": 15})
        return collect(list_of(data_of(body).get("info")), normalize_playlist, 15)

    async def top_card(self, type_: str = "") -> list[dict]:
        await self.ensure_device()
        path = "/top/card" + (f"/{type_}" if type_ else "")
        body = await self.request(path, {})
        return collect(list_of(body.get("data")), normalize_song, 20)

    async def rank_top(self) -> list[dict]:
        """编辑推荐的榜单（/rank/top），行结构与 rank_list 一致，可直接展开。"""
        await self.ensure_device()
        body = await self.request("/rank/top", self._ts({}))
        items = list_of(data_of(body).get("list"))
        return [
            {
                "index": i + 1,
                "id": str(it.get("rankid") or ""),
                "name": it.get("rankname") or "",
                "sub": it.get("update_frequency") or it.get("intro") or "",
                "cover": str(it.get("imgurl") or it.get("img_9") or "").replace("{size}", "300"),
                "tag": "",
            }
            for i, it in enumerate(items)
            if isinstance(it, dict) and it.get("rankid")
        ][:12]

    async def top_ip(self) -> list[dict]:
        """编辑精选专题（/top/ip）：卡片列表，只展示不可播。"""
        await self.ensure_device()
        body = await self.request("/top/ip", {})
        items = list_of(data_of(body).get("list"))
        return [
            {
                "index": i + 1,
                "name": it.get("title") or "",
                "sub": it.get("sub_title") or "",
                "cover": str(it.get("image_url") or it.get("sizable_image_url") or "").replace(
                    "{size}", "300"
                ),
                "id": "",
            }
            for i, it in enumerate(items)
            if isinstance(it, dict) and it.get("title")
        ][:15]

    async def yueku_songs(self, limit: int = 30) -> list[dict]:
        """乐库推荐歌（/yueku）：info.song 只有 1 首，把 vip_music.list 一并合并。"""
        await self.ensure_device()
        body = await self.request("/yueku", {})
        data = data_of(body)
        info = data.get("info") if isinstance(data.get("info"), dict) else {}
        items = list_of(info.get("song"))
        vip = info.get("vip_music") if isinstance(info.get("vip_music"), dict) else {}
        items += list_of(vip.get("list"))
        seen: set[str] = set()
        merged = []
        for it in items:
            if isinstance(it, dict):
                h = str(it.get("hash") or "")
                if h and h in seen:
                    continue
                seen.add(h)
            merged.append(it)
        return collect(merged, normalize_song, limit)

    # ──────────── 歌单 / 专辑 / 歌手 ────────────
    async def playlist_songs(self, pid: str, limit: int = 30) -> tuple[list[dict], str, str]:
        """返回 (songs, 歌单名, 歌单封面)。"""
        await self.ensure_device()
        body = await self.request("/playlist/track/all", self._ts({"id": pid, "pagesize": limit, "page": 1}))
        data = data_of(body)
        songs = list_of(data.get("songs"))
        info = data.get("list_info") if isinstance(data.get("list_info"), dict) else {}
        name = str(info.get("name") or "")
        cover = str(info.get("img") or info.get("pic") or "").replace("{size}", "400")
        return collect(songs, normalize_song, limit), name, cover

    async def playlist_tags(self) -> list[str]:
        """歌单分类：父分类与 son 拍平（酷狗是两级树，展示取名字即可）。"""
        await self.ensure_device()
        body = await self.request("/playlist/tags")
        out = []
        for node in list_of(body.get("data")):
            if not isinstance(node, dict):
                continue
            if node.get("tag_name"):
                out.append(str(node["tag_name"]))
            for son in list_of(node.get("son")):
                if isinstance(son, dict) and son.get("tag_name"):
                    out.append(str(son["tag_name"]))
        return out[:60]

    async def theme_playlists(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/theme/playlist", {})
        return collect(list_of(data_of(body).get("lists") or body.get("data")), normalize_playlist, 15)

    async def album_songs(self, aid: str, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/album/songs", {"id": aid, "pagesize": limit})
        return collect(list_of(data_of(body).get("songs")), normalize_song, limit)

    async def artist_songs(self, artist_id: str, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/artist/audios", {"id": artist_id, "pagesize": limit, "page": 1})
        return collect(list_of(body.get("data")), normalize_song, limit)

    async def artist_albums(self, artist_id: str, limit: int = 15) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/artist/albums", {"id": artist_id, "pagesize": limit})
        return collect(list_of(body.get("data")), normalize_album, limit)

    async def artist_lists(self, type_: int = 0) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/artist/lists", {"type": type_, "hotsize": 20})
        out = []
        idx = 0
        for sec in list_of(data_of(body).get("info")):
            if not isinstance(sec, dict):
                continue
            for a in sec.get("singer") or []:
                norm = normalize_artist(a, idx)
                if norm:
                    idx += 1
                    out.append(norm)
        return out

    # ──────────── 推荐 / FM ────────────
    async def everyday_recommend(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/everyday/recommend", self._ts({}))
        data = data_of(body)
        return collect(list_of(data.get("song_list") or data.get("songs")), normalize_song, 30)

    async def personal_fm(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/personal/fm", self._ts({}))
        return collect(list_of(data_of(body).get("song_list")), normalize_song, 10)

    # ──────────── 评论 / MV ────────────
    async def comments(self, mixsongid: str, limit: int = 12, kind: str = "music") -> dict:
        await self.ensure_device()
        path = {"music": "/comment/music", "playlist": "/comment/playlist", "album": "/comment/album"}[kind]
        params = {"page": 1, "pagesize": limit}
        # /comment/music 认 mixsongid；歌单/专辑评论的参数名是 id（module 里 childrenid: params.id）
        if kind == "music":
            params["mixsongid"] = mixsongid
        else:
            params["id"] = mixsongid
        body = await self.request(path, params)
        return {
            "hot": collect(list_of(body.get("list")), self._norm_comment, limit),
            "new": [],
            "total": int(num(body.get("count"))),
        }

    @staticmethod
    def _norm_comment(item: dict, idx: int = 0) -> dict | None:
        if not isinstance(item, dict):
            return None
        content = str(item.get("content") or "").replace("\r", " ").strip()
        if not content:
            return None
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        like = item.get("like") if item.get("like") is not None else item.get("praise")
        ts = num(item.get("time"))
        time_str = time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts > 100000000 else ""
        return {
            "index": idx + 1,
            "nick": user.get("nickname") or item.get("nickname") or "酷狗用户",
            "avatar": str(item.get("avatar") or user.get("avatar") or "").replace("{size}", "100"),
            "time": time_str,
            "likes": int(num(like)),
            "content": content[:300],
            "hot": bool(item.get("is_hot") or item.get("hot")),
        }

    async def mv_url(self, hash_: str) -> dict:
        await self.ensure_device()
        body = await self.request("/video/url", {"hash": hash_})
        data = data_of(body)
        node = data.get(str(hash_).lower()) if isinstance(data.get(str(hash_).lower()), dict) else {}
        return {"url": str(node.get("downurl") or "")}

    async def song_mv_url(self, song: dict) -> dict:
        """统一协议：酷狗歌曲结果不携带 MV 标识（normalize_song 的 mvid 恒空），
        无法由歌曲反查 MV —— 返回空 url 让上层走「没有关联 MV」文案。
        能取到 MV 流的只有 MV 搜索结果（explore 的 mv 条目自带 MVHash，直接调 mv_url）。
        """
        return {"url": ""}

    async def mv_search(self, keyword: str, limit: int = 8) -> list[dict]:
        self.require_login()
        await self.ensure_device()
        body = await self.request("/search", {"keywords": keyword, "type": "mv", "pagesize": limit})
        out = []
        for i, it in enumerate(list_of(data_of(body).get("lists"))):
            if not isinstance(it, dict):
                continue
            name = str(it.get("MVName") or it.get("name") or "")
            if not name:
                continue
            out.append(
                {
                    "index": i + 1,
                    "id": str(it.get("MVHash") or it.get("hash") or it.get("hash_mvid") or ""),
                    "name": name,
                    "artist": _singers(it),
                    "cover": _cover_of(it).replace("{size}", "300"),
                    "duration": _dur(num(it.get("Duration") or it.get("timelength"))),
                    "playCount": 0,
                }
            )
        return out

    # ---- 用户（需登录）----
    async def login_status(self) -> dict:
        if not self.cookie:
            return {"loggedIn": False, "uid": "", "nickname": "", "avatar": ""}
        await self.ensure_device()
        try:
            body = await self.request("/user/detail", self._ts({}))
            data = data_of(body)
            userinfo = data.get("userinfo") if isinstance(data.get("userinfo"), dict) else {}
            return {
                "loggedIn": bool(userinfo),
                "uid": str(self._config.src_uid("kg") or ""),
                "nickname": str(userinfo.get("nickname") or ""),
                "avatar": str(userinfo.get("avatar") or "").replace("{size}", "100"),
            }
        except ApiError as e:
            if opt_int(e.code) in (20010, 20017, 35002):
                return {"loggedIn": False, "uid": "", "nickname": "", "avatar": ""}
            raise

    async def vip_info(self) -> dict:
        await self.ensure_device()
        body = await self.request("/user/vip/detail", self._ts({}))
        data = data_of(body)
        row = data.get("row") if isinstance(data.get("row"), list) else []
        level = 0
        expire = ""
        for it in row:
            if isinstance(it, dict) and it.get("token"):
                level = max(level, int(num(it.get("level"))))
                expire = str(it.get("end_time") or "")
        return {"vipLevel": level, "expire": expire}

    async def user_grade(self) -> dict:
        await self.ensure_device()
        body = await self.request("/user/grade/info", self._ts({}))
        data = data_of(body)
        return {"p_grade": int(num(data.get("p_grade"))), "d_sec": num(data.get("d_sec"))}

    async def user_playlists(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/user/playlist", self._ts({}))
        data = data_of(body)
        return collect(list_of(data.get("list") or data.get("info")), normalize_playlist, 30)

    async def recent_songs(self, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/user/listen", self._ts({}))
        songs = list_of(data_of(body).get("songs") or body.get("data"))
        return collect(songs, normalize_song, limit)

    async def history_recommend(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/everyday/history", self._ts({"mode": "song"}))
        data = data_of(body)
        return collect(list_of(data.get("song_list") or data.get("songs")), normalize_song, 30)

    async def daily_recommend(self) -> list[dict]:
        """日推（与 ncm/qq 客户端同名；酷狗对应 /everyday/recommend）。"""
        return await self.everyday_recommend()

    async def cloud_songs(self, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/user/cloud", {"page": 1, "pagesize": limit})
        data = data_of(body)
        out = []
        for i, it in enumerate(list_of(data.get("list"))):
            if not isinstance(it, dict):
                continue
            out.append(
                {
                    "index": i + 1,
                    "sid": str(it.get("hash") or ""),
                    "name": it.get("songname") or it.get("filename") or "",
                    "artist": it.get("singername") or "",
                    "album": it.get("albumname") or "",
                    "size": f"{int(num(it.get('filesize')) / 1024 / 1024 * 10) / 10:.1f}MB",
                }
            )
        return out

    async def purchased_songs(self, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/user/purchased/songs", {})
        return collect(
            list_of(data_of(body).get("lists") or data_of(body).get("song_list")), normalize_song, limit
        )

    async def follow_artist(self, artist_id: str, follow: bool) -> str:
        await self.ensure_device()
        await self.request("/artist/follow" if follow else "/artist/unfollow", self._ts({"id": artist_id}))
        return "已关注歌手" if follow else "已取消关注"

    async def followed_new_songs(self, limit: int = 30) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/artist/follow/newsongs", {})
        return collect(list_of(data_of(body).get("songs") or body.get("data")), normalize_song, limit)

    # ──────────── 登录 ────────────
    async def qr_key(self) -> str:
        body = await self.request("/login/qr/key", self._ts({}))
        data = data_of(body)
        key = data.get("qr_key") or data.get("key") or ""
        if not key:
            raise ApiError("获取二维码 Key 失败", source="kg")
        return str(key)

    async def qr_create(self, key: str) -> dict:
        body = await self.request("/login/qr/create", self._ts({"key": key, "qrimg": "true"}))
        data = data_of(body)
        # KuGouMusicApi 返回 {url: 扫码页链接, base64: dataURL 图片}；旧键名兜底兼容其他分叉
        return {
            "qrcode": data.get("url") or data.get("qrcode") or "",
            "qrimg": data.get("base64") or data.get("qrimg") or data.get("image") or "",
        }

    async def qr_check(self, key: str) -> dict:
        """status: 0 过期 / 1 待扫 / 2 已扫待确认 / 4 成功（带 cookie）。"""
        body = await self.request("/login/qr/check", self._ts({"key": key}))
        data = data_of(body)
        status = opt_int(data.get("status"))
        out = {
            "code": status,
            "cookie": "",
            "uid": "",
            "nickname": "",
            "msg": {0: "二维码已过期", 1: "等待扫码", 2: "已扫码，请在手机上确认", 4: "登录成功"}.get(
                status, f"状态 {status}"
            ),
        }
        if status == 4:
            # 凭证来源：优先上游回传的 cookie 串；否则用 data 里的 token/userid 等拼装
            cookie = str(body.get("cookie") or data.get("cookie") or "")
            if not cookie:
                pairs = [
                    f"{k}={data[k]}" for k in ("token", "userid", "vip_type", "vip_token") if data.get(k)
                ]
                cookie = "; ".join(pairs)
            out["cookie"] = cookie
            out["uid"] = str(data.get("userid") or "")
            out["nickname"] = str(data.get("nickname") or "")
        return out


register("kg", lambda config, kg_device_path=None, **kw: KugouClient(config, kg_device_path))
