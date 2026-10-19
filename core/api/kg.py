"""酷狗音乐音源客户端（基于自建 KuGouMusicApi HTTP 服务）。

- 失败响应统一 HTTP 502 + error_code；业务成功看 body.status==1，status==2 是
  「该音质不可用」的降级信号
- 设备 dfid 通过 /register/dev 注册并持久化（data/plugin_data/<plugin>/device_cookies.json）
- 酷狗每个音质一个专属 hash，取链按档位走对应 hash（见 quality.kg_hash_for）
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from astrbot.api import logger

from ..errors import KG_ERRORS, ApiError, NotEnabledError
from ..quality import KG_LABEL, first, kg_hash_for, ladder_for
from .http import (
    collect,
    data_of,
    format_duration,
    get_session,
    harden_sensitive_file,
    list_of,
    num,
    opt_int,
    request_json,
    with_ts,
)
from .registry import register

# 旧调用点的模块级别名：与 http.format_duration 同一实现，勿再复制
_dur = format_duration

# /top/song 新歌速递的榜单 type 值（酷狗上游协议魔数，rank 列表里「新歌」条目的 id）
KG_NEW_SONG_RANK_ID = 21608


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
    # 高档 hash 的子对象键大小写两种都出现过（同 filename 的口径，见上）：
    # 只认大写 Hash 会让高档 hash 取空 → 阶梯静默降到 128
    hq, sq = _sub(item, "HQ"), _sub(item, "SQ")
    tp = item.get("trans_param") if isinstance(item.get("trans_param"), dict) else {}
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
        # trans_param 兜底：搜索条目常无 audio_info/HQ/SQ（高档 hash 只在
        # trans_param.ogg_320_hash / ogg_flac_hash 里），漏读会让 kg_hash_for 取空
        # → song_url_best 静默跳档，用户配了 flac 也恒播 128（与 _cover_of 同源取法）
        "hash_320": str(
            ainfo.get("hash_320")
            or item.get("hash_320")
            or hq.get("Hash")
            or hq.get("hash")
            or tp.get("ogg_320_hash")
            or ""
        ),
        "hash_flac": str(
            ainfo.get("hash_flac")
            or item.get("hash_flac")
            or sq.get("Hash")
            or sq.get("hash")
            or tp.get("ogg_flac_hash")
            or ""
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


def normalize_playlist(item: dict, idx: int = 0) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("specialname") or item.get("name") or item.get("special_name") or ""
    if not name:
        return None
    return {
        "index": idx + 1,
        "source": "kg",
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
        "source": "kg",
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
        "source": "kg",
        "id": str(
            item.get("AuthorId") or item.get("author_id") or item.get("singerid") or item.get("id") or ""
        ),
        "name": str(name),
        "cover": cover.replace("{size}", "300"),
        "sub": "",
    }


class KugouClient:
    source = "kg"

    # 注册失败后的冷却时长：期间直接重抛，不再打 /register/dev（防限频正反馈）
    _DEVICE_BACKOFF = 60.0

    def __init__(self, config, device_path: Path):
        self._config = config
        self._device_path = device_path  # device_cookies.json
        self._device_cookie = ""
        self._device_lock = asyncio.Lock()  # 并发首调只放一个进 /register/dev
        self._device_fail_ts: float | None = None  # 注册失败冷却起点（monotonic）；None=从未失败
        # 20028/20040 设备自愈只做一次：重注册后仍被拒时反复清 cookie 只会放大风控
        self._device_reset_done = False

    def ready(self) -> bool:
        """无重依赖（走 HTTP 服务），配置了地址即可用。"""
        return bool(self.base)

    # ──────────── 设备 ────────────
    def load_device(self) -> None:
        try:
            if not self._device_path:
                # 工厂漏传 kg_device_path 时 None.read_text 是 AttributeError，
                # 不在下方 except 捕获面里——显式拒绝比误写「None 文件」清晰
                raise ValueError("kg 设备路径未配置")
            data = json.loads(self._device_path.read_text("utf-8"))
            # 手工编辑/损坏可能产生合法 JSON 但非 dict（"dfid=..."、[]、null）：
            # data.get 会抛 AttributeError 且不在捕获面内，initialize 阶段会拖垮整个插件
            if not isinstance(data, dict):
                raise ValueError("kg 设备文件不是 JSON 对象")
            self._device_cookie = str(data.get("cookie") or "")
        except (OSError, ValueError):
            self._device_cookie = ""

    def _jar_cookie(self) -> str:
        """注册响应后从共享会话 cookie jar 取设备 cookie 串（按键名白名单过滤）。

        KuGouMusicApi 服务端会把 dfid + KUGOU_API_GUID/MID/DEV… 经 Set-Cookie 回写，
        request.js 签名依赖这些键 —— 只保留 dfid/KUGOU_API_*：cookie jar 按 host
        隔离不分端口，同主机部署时（127.0.0.1:3000 网易 + :4000 酷狗）其他音源
        服务的活凭证（如 MUSIC_U）也会出现在该 host 的 jar 里，整串落盘等于把
        无关凭证复制进 device_cookies.json 还随每次请求外发。
        """
        try:
            from yarl import URL

            jar = get_session().cookie_jar
            cookies = jar.filter_cookies(URL(self.base))
            parts = [
                f"{k}={m.value}" for k, m in cookies.items() if k == "dfid" or k.startswith("KUGOU_API_")
            ]
            return "; ".join(p for p in parts if p)
        except Exception:  # noqa: BLE001 - jar 读取失败退回仅 dfid 的旧行为
            return ""

    async def ensure_device(self) -> str:
        """设备 cookie（dfid=... + KUGOU_API_*）。注册接口限频且静默失败：连发只有
        第一次返回 dfid，其余返回空 data —— 必须判断 dfid 存在并复用缓存。

        磁盘读写走 to_thread：本方法被大量接口调用，裸同步 IO 会卡住整个事件循环。

        失败冷却：search/suggest/comments 等 30+ 方法都以 ensure_device() 开头，失败后
        不设冷却会维持限频窗口、形成正反馈 —— 冷却期内直接重抛同一错误不再打注册接口。
        """
        if self._device_cookie:
            return self._device_cookie
        async with self._device_lock:
            if self._device_cookie:  # 双检：等锁期间可能已被并发调用方填好
                return self._device_cookie
            # 哨兵判据：_device_fail_ts 为 None 表示从未失败过。monotonic() 的参考点
            # 多取开机时刻，开机 60 秒内启动插件时 `BACKOFF - (mono - 0.0)` 恒为正，
            # 会凭空报「设备注册失败 X 秒后重试」
            if (
                self._device_fail_ts is not None
                and (wait := self._DEVICE_BACKOFF - (time.monotonic() - self._device_fail_ts)) > 0
            ):
                raise ApiError(
                    f"设备注册失败：未拿到 dfid（上游静默限频），{int(wait) + 1} 秒后重试", source="kg"
                )
            await asyncio.to_thread(self.load_device)
            if self._device_cookie:
                return self._device_cookie
            # timestamp 穿透服务端按完整 URL 的 2 分钟缓存（POST 同样缓存）：否则
            # 冷却期结束后的重试会命中上一次的静默限频失败体（data:[]），冷却形同虚设
            body = await self.request("/register/dev", self._ts({}), inject_cookie=False)
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
                self._device_fail_ts = time.monotonic()
                raise ApiError("设备注册失败：未拿到 dfid（上游静默限频），稍后重试", source="kg")
            self._device_fail_ts = None
            # jar 读取失败（_jar_cookie 异常返回空串）会拼出纯 dfid 串：内存里本次可用
            # （服务端每请求都会重新 Set-Cookie），但持久化等于落盘「缺 KUGOU_API_*、
            # 重启触发 20028」的坏缓存 —— 与本模块 docstring 的风控约定矛盾，跳过落盘
            if "KUGOU_API_" not in cookie:
                self._device_cookie = cookie
                return cookie
            await asyncio.to_thread(self._persist_device, cookie, dfid)
            self._device_cookie = cookie
            return cookie

    def _persist_device(self, cookie: str, dfid: str) -> None:
        """落盘设备 cookie（同步，供 to_thread 调用）。落盘失败必须留痕：静默失败
        意味着每次重启都重新注册设备，正是 docstring 要避免的风控放大场景。

        dfid + KUGOU_API_* 是签名必需的完整设备身份（风控敏感数据），落盘后与 QQ
        设备指纹同口径做 best-effort 权限收紧。"""
        try:
            self._device_path.parent.mkdir(parents=True, exist_ok=True)
            self._device_path.write_text(
                json.dumps({"cookie": cookie, "dfid": dfid, "ts": int(time.time())}, ensure_ascii=False),
                "utf-8",
            )
        except OSError as e:
            logger.warning(f"[music_hub] 酷狗设备 cookie 落盘失败（重启后需重新注册）: {e}")
            return
        harden_sensitive_file(self._device_path)

    async def _reset_device_once(self) -> None:
        """已带设备 cookie 仍被 20028/20040 拒绝时的一次性自愈：清内存并删落盘文件，
        让下一轮 ensure_device 自动重新注册——服务端换设备密钥后旧 cookie 必 20028，
        只能重注册自愈，不该再让用户手动删 device_cookies.json。
        一次性守卫：新设备仍被拒时反复重注册只会放大风控，故每进程只清一次。"""
        if self._device_reset_done or not self._device_cookie:
            return
        self._device_reset_done = True
        self._device_cookie = ""
        if not self._device_path:
            return
        try:
            await asyncio.to_thread(self._device_path.unlink)
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
        # noCookie=true：上游不把模块 cookie 回写进响应 Set-Cookie。共享会话 jar
        # 一旦收录登录 token（扫码成功响应的 Set-Cookie 带 token/userid），登出
        # 只清配置不清 jar，残留的旧 token 会随后续请求原样送达上游（酷狗对
        # Cookie 头是按键合并、无同名键覆盖）——已登出账号在插件重载前仍是事实
        # 登录态，取流/VIP/关注写操作全部按旧账号生效。插件所有请求都显式传
        # cookie 串、从不消费响应 Set-Cookie，唯一例外是 /register/dev：设备
        # cookie（dfid/KUGOU_API_*）全靠 jar 收录后经 _jar_cookie 持久化
        if pathname != "/register/dev":
            params["noCookie"] = "true"
        status, body = await request_json("kg", "酷狗 API", base, pathname, params, method=method)
        try:
            return self._handle(body, status)
        except ApiError as e:
            # 已带设备 cookie 仍被 20028（风控/设备失效）/20040 拒绝：设备身份已失效，
            # 清掉让下一轮 ensure_device 自动重新注册。挂统一出口而不是只挂 song_url：
            # 纯浏览路径（搜索/歌词/评论）命中时同样自愈，否则要等某次点歌取流才恢复。
            # 错误照常上抛由各调用方归因
            if opt_int(e.code) in (20028, 20040):
                await self._reset_device_once()
            raise

    def _handle(self, body: dict, status: int) -> dict:
        """业务级错误映射。传输/解析层（网络异常、非 JSON）在 http.request_json；
        status==0 是 KuGouMusicApi 的上游故障约定（HTTP 可能仍是 502/200）。"""
        # status 用字符串口径比较：自建服务分叉实现可能回 "0"（字符串），
        # 严格 == 0 会把上游故障误判成业务成功、走进字段解析炸出难懂的 KeyError
        # ssa-code 二次验证（滑块/短信）：上游 request.js 在成功与失败两条路径都把
        # 该头注入响应体 ssaCode 字段——命中时换任何接口重试同样被拦，给出可行动
        # 文案而不是泛化错误（成功响应也要拦：字段解析会拿到空数据误导为「无结果」）
        if body.get("ssaCode"):
            raise ApiError(
                "酷狗触发风控，需要二次安全验证（滑块/短信）：请在酷狗 App 完成验证后重试",
                source="kg",
                payload=body,
            )
        if status >= 400 or str(body.get("status")) == "0":
            code = body.get("error_code") or body.get("errcode") or body.get("err_code")
            msg = _upstream_msg(body)
            c = opt_int(code)
            err_msg = str(msg) or (KG_ERRORS.get(c, "") if c else "") or f"酷狗 API 错误（HTTP {status}）"
            raise ApiError(err_msg, code=c, source="kg", payload=body)
        return body

    # 实现收敛到 http（with_ts / format_duration）；保留旧名以维持内部调用点
    # （_ts 遍布全文件）与 _dur 的既有别名不漂移
    _ts = staticmethod(with_ts)
    _dur = staticmethod(format_duration)

    # ──────────── 搜索 ────────────
    def require_login(self, what: str = "搜索") -> None:
        """酷狗已禁止匿名搜索（error_code=152）：占位 cookie 与设备 cookie 均无效。

        未登录时提前拦截，避免发出注定失败的请求，也让上层能给出扫码引导。
        浏览类端点（日推/云盘/最近在听等）匿名一律 20010 泛化文案，同样在此
        统一提前拦出口径；what 仅用于文案点出功能名。
        """
        if not self.cookie:
            # 不带 code：错误码表里 20010 是泛化文案，会盖掉这里的扫码引导（errors.user_msg 优先查表）
            raise ApiError(f"酷狗{what}需登录，请先发送「kg登录」扫码", source="kg")

    async def search(self, keyword: str, limit: int = 10, type_: str = "song", page: int = 1) -> list[dict]:
        """page 从 1 起（ncm 是 0 起）——三源 page 语义不统一，调用方传值时注意。"""
        self.require_login()
        await self.ensure_device()
        try:
            body = await self.request(
                "/search", {"keywords": keyword, "type": type_, "pagesize": limit, "page": page}
            )
        except ApiError as e:
            # 152 = 上游按匿名拒绝搜索。走到这里必然已带登录 cookie（require_login
            # 已拦住无 cookie 的调用）：token 被上游拒绝——常见于标准版/概念版
            # 部署不匹配、服务端重启换设备密钥、或登录态过期。面板账号页会一直
            # 显示「已登录」（只看 cookie 存在性），没有这条提示用户无从下手。
            # 不带 code：错误码表 152 是泛化文案，user_msg 优先查表会盖掉扫码引导
            if str(e.code) == "152":
                raise ApiError(
                    "酷狗登录态已失效（token 被上游拒绝，常见于 API 服务重启或平台版本不匹配），"
                    "请发送「kg登录」重新扫码",
                    source="kg",
                ) from e
            raise
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
        # 与文件内其他方法一致先 ensure_device：设备未注册时请求缺 dfid/
        # 签名键，会触发 20028（本方法原先唯二漏调，另一个是 hot_search）
        await self.ensure_device()
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
        await self.ensure_device()
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
        # extName 统一无点小写，与 qq/ncm 的 ext 字段同格式
        ext = str(body.get("extName") or "").lstrip(".").lower()
        return {"url": url, "status": body.get("status"), "extName": ext, "raw": body}

    @staticmethod
    def _is_trial_url(url: str) -> bool:
        """凭文件名形态猜试听片段：URL 含 p_0_ 且不含 full 视为 60s 试听。

        纯启发式，无上游出处佐证（已知局限）：误判会把整曲标成试听（卡片错标
        「试听 60s」、队列提前切歌）；漏判则试听片段被当整曲播，60s 后断流。
        """
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
                # 会话级失败（需登录/未登录/风控/设备失效）换音质档重试同样必失败：
                # 20028/20040 的设备自愈已在 request 出口先行触发，这里短路只是
                # 不再空烧剩余档位——对齐 ncm/qq 的会话级短路口径
                if opt_int(e.code) in (20010, 20017, 20028, 20040):
                    raise
                last_err = e
                continue
            # status 与 _handle 同口径走宽松整型：分叉上游回 "1"（字符串）时严格
            # ==1 会把已拿到的有效 URL 当失败丢弃，错误归因也跟着错
            status = opt_int(r.get("status"))
            if r.get("url") and status == 1:
                trial = self._is_trial_url(r["url"])
                return {
                    "url": r["url"],
                    "quality": "128" if trial else q,
                    "qualityLabel": KG_LABEL.get("128", "") if trial else KG_LABEL.get(q, q),
                    "trial": trial,
                    "extName": r.get("extName") or "",
                    "ext": r.get("extName") or "",
                    "raw": r.get("raw"),
                }
            if status == 2:
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
                    # free_part=1 只对部分歌曲生效：上游可能照样回整曲 URL，不验证
                    # 就标 trial=True 会让队列按 60s 误切歌（与 _is_trial_url 的漏判
                    # 方向互补）。URL 判不出试听形态时按整曲 128 档返回
                    if r.get("url") and opt_int(r.get("status")) == 1:
                        trial = self._is_trial_url(r["url"])
                        return {
                            "url": r["url"],
                            "quality": "128",
                            "qualityLabel": "试听 60s" if trial else KG_LABEL.get("128", "128"),
                            "trial": trial,
                            "extName": r.get("extName") or "",
                            "ext": r.get("extName") or "",
                            "raw": r.get("raw"),
                        }
        if last_err is not None and opt_int(last_err.code) in (20010, 20017, 20028, 20040):
            raise last_err
        if last_paid:
            # status==2 只说明「该音质不可用」，免费歌部分档位不可用也会置位——
            # 归因模糊时两种可能都交代，避免把免费歌误导成「去扫码登录」。
            # 不带 code=20017：那在 KG_ERRORS 表里会被 user_msg() 覆盖成登录失效文案
            raise ApiError("未能获取播放链接：歌曲可能为付费/VIP，或当前可用音质均不可用", source="kg")
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
        """song 协议：普通歌词（LRC）。候选定位 → 取内容两步在源内闭环。

        空内容跳过试下一个候选（与 song_lyric_karaoke 同纪律）：首个候选
        （常按 hash 定位）decodeContent 为空不代表其余候选也没有。
        单个候选的 accesskey 失效等 ApiError 同样只放弃该候选——它是「这条线索
        取不到」，不是「整首歌没有歌词」。"""
        for c in await self._locate_candidates(song):
            if c.get("id") and c.get("accesskey"):
                try:
                    content = await self.lyric(c["id"], c["accesskey"], "lrc")
                except ApiError:
                    continue
                if content:
                    return {"lrc": content, "tlyric": "", "yrc": ""}
        return {"lrc": "", "tlyric": "", "yrc": ""}

    async def song_lyric_karaoke(self, song: dict) -> dict:
        """song 协议：逐字歌词（KRC）。无候选或全无 accesskey 时返回空 yrc。
        单候选 ApiError 跳过试下一个，理由同 song_lyric。"""
        for c in await self._locate_candidates(song):
            if c.get("id") and c.get("accesskey"):
                try:
                    content = await self.lyric(c["id"], c["accesskey"], "krc")
                except ApiError:
                    continue
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
            # 只认本曲 hash 的键：兜底整个 data 会把同响应里别首歌的高潮片段当本曲返回
            d0 = data.get(hash_) or data.get(str(hash_).lower())
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

    async def new_songs(self, rank_id: int = KG_NEW_SONG_RANK_ID) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/top/song", {"type": rank_id})
        return collect(list_of(body.get("data")), normalize_song, 20)

    async def top_playlists(self) -> list[dict]:
        await self.ensure_device()
        body = await self.request("/top/playlist", {"pagesize": 15})
        return collect(list_of(data_of(body).get("info")), normalize_playlist, 15)

    async def top_card(self, type_: str = "") -> list[dict]:
        await self.ensure_device()
        # type_ 拼进 pathname 前先过白名单式校验：当前唯一调用点无参传入，
        # 防将来接入用户输入时变成路径注入
        seg = f"/{type_}" if re.fullmatch(r"[0-9A-Za-z_-]{1,32}", str(type_) or "") else ""
        body = await self.request("/top/card" + seg, {})
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
        self.require_login("每日推荐")
        await self.ensure_device()
        body = await self.request("/everyday/recommend", self._ts({}))
        data = data_of(body)
        return collect(list_of(data.get("song_list") or data.get("songs")), normalize_song, 30)

    async def personal_fm(self) -> list[dict]:
        self.require_login("私人 FM")
        await self.ensure_device()
        body = await self.request("/personal/fm", self._ts({}))
        return collect(list_of(data_of(body).get("song_list")), normalize_song, 10)

    # ──────────── 评论 / MV ────────────
    async def comments(self, mixsongid: str, limit: int = 12, kind: str = "music") -> dict:
        await self.ensure_device()
        path = {"music": "/comment/music", "playlist": "/comment/playlist", "album": "/comment/album"}.get(
            kind
        )
        if not path:
            raise ApiError(f"不支持的评论类型：{kind}", source="kg")
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
        # ≥1e11 视为毫秒（13 位），否则按秒：1e8 的旧阈值对秒/毫秒毫无区分力，
        # 秒级时间戳会被 /1000 显示成 1970 年
        time_str = (
            time.strftime("%Y-%m-%d", time.localtime(ts / 1000 if ts > 10**11 else ts)) if ts > 0 else ""
        )
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
        # 酷狗 hash 规范为小写：入参先统一小写再发请求与查响应键——
        # 大写入参时请求原样发出、响应键却按 lower 查，必然 miss 空返回
        h = str(hash_ or "").strip().lower()
        body = await self.request("/video/url", {"hash": h})
        data = data_of(body)
        node = data.get(h) if isinstance(data.get(h), dict) else {}
        if not node and h != str(hash_ or ""):
            # 服务端按原样回显键的兜底
            node = data.get(str(hash_ or "")) if isinstance(data.get(str(hash_ or "")), dict) else {}
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
                    # timelength 恒为毫秒（见 normalize_song 的 _dur 注释），混入 auto
                    # 阈值会把 <10s 的条目格式化成 150:00，显式按 ms；Duration 单位
                    # 未知，保持 auto 猜测
                    "duration": _dur(num(it.get("Duration"))) or _dur(num(it.get("timelength")), "ms"),
                    "playCount": 0,
                }
            )
        return out

    # ---- 用户（需登录）----
    async def login_status(self) -> dict:
        if not self.cookie:
            return {"loggedIn": False, "uid": "", "nickname": "", "avatar": ""}
        try:
            await self.ensure_device()
        except ApiError:
            # 设备注册限频/失败不挡状态展示：本方法只用于账号页展示「本地有无
            # cookie」，cookie 存在性判断不需要设备；上游昵称核验等下一轮刷新自会补上，
            # 挡在这里会让账号页在 60s 冷却窗内显示「设备注册失败」而非登录状态
            return {
                "loggedIn": True,
                "uid": str(self._config.src_uid("kg") or ""),
                "nickname": "",
                "avatar": "",
            }
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
            if opt_int(e.code) == 20018:
                # /user/detail 在凭证缺失/失效/平台不匹配时上游实际回 20018（实测，
                # 垃圾与空 cookie 同码）：归一为「凭证无效」而非透传——透传会让账号页
                # 显示「查询失败」，与总览页「已登录」（配置口径）矛盾，用户无从下手
                return {"loggedIn": False, "uid": "", "nickname": "凭证已失效，请重新扫码", "avatar": ""}
            raise

    async def vip_info(self) -> dict:
        # 设备注册冷却不挡 VIP 展示（与 login_status 同口径）：/user/vip/detail
        # 走已登录请求，设备 cookie 已在 _compose_cookie 里；裸调 ensure_device
        # 会在 60s 冷却窗内让账号页 VIP 行缺失（下游 vip_summary 吞异常归空）
        try:
            await self.ensure_device()
        except ApiError:
            pass
        body = await self.request("/user/vip/detail", self._ts({}))
        data = data_of(body)
        row = data.get("row") if isinstance(data.get("row"), list) else []
        level = 0
        expire = ""
        for it in row:
            if isinstance(it, dict) and it.get("token"):
                # level 与 expire 必须取自同一行：只在行级更高时整体覆盖——
                # `>=` 会让同级多行的后一行（含 end_time 为空的行）无条件
                # 清掉已有到期日，与「取自同一行」的意图相悖
                row_level = int(num(it.get("level")))
                if row_level > level:
                    level = row_level
                    expire = str(it.get("end_time") or "")
                elif row_level == level and not expire:
                    expire = str(it.get("end_time") or "")
        return {"vipLevel": level, "expire": expire}

    async def user_grade(self) -> dict:
        await self.ensure_device()
        body = await self.request("/user/grade/info", self._ts({}))
        data = data_of(body)
        return {"p_grade": int(num(data.get("p_grade"))), "d_sec": num(data.get("d_sec"))}

    async def user_playlists(self) -> list[dict]:
        self.require_login("浏览歌单")
        await self.ensure_device()
        body = await self.request("/user/playlist", self._ts({}))
        data = data_of(body)
        return collect(list_of(data.get("list") or data.get("info")), normalize_playlist, 30)

    async def recent_songs(self, limit: int = 30) -> list[dict]:
        self.require_login("查看最近在听")
        await self.ensure_device()
        body = await self.request("/user/listen", self._ts({}))
        songs = list_of(data_of(body).get("songs") or body.get("data"))
        return collect(songs, normalize_song, limit)

    async def history_recommend(self) -> list[dict]:
        self.require_login("历史推荐")
        await self.ensure_device()
        body = await self.request("/everyday/history", self._ts({"mode": "song"}))
        data = data_of(body)
        return collect(list_of(data.get("song_list") or data.get("songs")), normalize_song, 30)

    async def daily_recommend(self) -> list[dict]:
        """日推（与 ncm/qq 客户端同名；酷狗对应 /everyday/recommend）。"""
        return await self.everyday_recommend()

    async def cloud_songs(self, limit: int = 30) -> list[dict]:
        self.require_login("云盘")
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
        self.require_login("已购音乐")
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
        self.require_login("关注歌手新歌")
        await self.ensure_device()
        body = await self.request("/artist/follow/newsongs", {})
        return collect(list_of(data_of(body).get("songs") or body.get("data")), normalize_song, limit)

    # ──────────── 登录 ────────────
    async def qr_key(self) -> str:
        body = await self.request("/login/qr/key", self._ts({}))
        data = data_of(body)
        # 现版 KuGouMusicApi 的 key 在 data.qrcode（响应里还直接带 qrcode_img 二维码图），
        # 旧版字段 qr_key / key 保留兜底，兼容未升级的自建服务
        key = data.get("qrcode") or data.get("qr_key") or data.get("key") or ""
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
                status, "未知状态" if status is None else f"状态 {status}"
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


def _create(config, kg_device_path=None, **kw):
    # device_path 是必传 kwarg（service 恒传）：漏传时 load_device 的显式拒绝会被
    # except (OSError, ValueError) 吞成空串、注册照发，成功后 _persist_device 对
    # None 抛 AttributeError——入口显式拒绝，报错可读（对齐 qq._create）
    if not kg_device_path:
        raise NotEnabledError("酷狗音源缺少设备 cookie 路径（kg_device_path）", source="kg")
    return KugouClient(config, kg_device_path)


register("kg", _create)
