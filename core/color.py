"""封面取色动态主题 + 三音源品牌色板。

流程：渲染层发现卡片数据里的封面 URL → 下载（复用插件 aiohttp 会话，8s）→
Pillow 量化取候选色 → 按饱和度/明度/占比打分选主色 → 生成整套满足 WCAG
对比度的色阶 → 写入 ``data["theme"]``，模板以 CSS 变量消费。

任一步失败返回 None，卡片保持音源品牌默认配色——主题只是增强，失败不影响出图。
"""

from __future__ import annotations

import asyncio
import colorsys
import time
from collections import OrderedDict
from urllib.parse import urlsplit

import aiohttp

from . import SOURCE_KG, SOURCE_NCM, SOURCE_QQ

# 三音源品牌默认色（与模板 :root 一致；muted 为加深后的无障碍版本）
BRANDS = {
    SOURCE_NCM: {
        "nm": "#ec4141",
        "nm_deep": "#c62f2f",
        "hero_a": "#ef4e44",
        "hero_b": "#d0362c",
        "card_a": "#fdf8f7",
        "card_b": "#f9e9e6",
        "soft": "#fbeeea",
        "muted": "#8a5a52",
        "text": "#3a1d18",
        "chip": "rgba(236, 65, 65, 0.09)",
        "line": "rgba(236, 65, 65, 0.16)",
    },
    SOURCE_KG: {
        "nm": "#2ca6e0",
        "nm_deep": "#1d84b8",
        "hero_a": "#33a9e0",
        "hero_b": "#1d7fc0",
        "card_a": "#f7fbfd",
        "card_b": "#e5f1f9",
        "soft": "#e9f4fb",
        "muted": "#4f6f82",
        "text": "#12303f",
        "chip": "rgba(44, 166, 224, 0.09)",
        "line": "rgba(44, 166, 224, 0.16)",
    },
    SOURCE_QQ: {
        "nm": "#31c27c",
        "nm_deep": "#1f9a64",
        "hero_a": "#36c580",
        "hero_b": "#1fa968",
        "card_a": "#f7fcf9",
        "card_b": "#e3f4eb",
        "soft": "#e9f8f0",
        "muted": "#5d7367",
        "text": "#143528",
        "chip": "rgba(49, 194, 124, 0.1)",
        "line": "rgba(49, 194, 124, 0.15)",
    },
}

DEFAULT_BRAND = BRANDS[SOURCE_NCM]


def brand_for(source: str) -> dict:
    return BRANDS.get(source, DEFAULT_BRAND)


_CACHE: dict[str, tuple[float, dict | None]] = {}
# 正在计算中的封面 → Future。同一张热门封面被 N 个并发请求同时命中未命中时，
# 只发起一次下载 + 一次 Pillow 量化，其余 await 同一个 Future。
_INFLIGHT: dict[str, asyncio.Future] = {}
_LOCK = asyncio.Lock()
_TTL = 600
_MAX = 128
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=8)
_MAX_BYTES = 8 * 1024 * 1024
_MAX_REDIRECTS = 5
# 解压炸弹阈值：4096×4096，远超任何正常封面分辨率
_MAX_PIXELS = 4096 * 4096


def _public_http_url(url: str) -> bool:
    """封面 URL 的内网预检：host 字面是回环/私网/链路本地地址时拒绝。

    封面字段来自音源 API / 分享链接解析（半可信第三方，恶意歌单可自定义封面），
    与 download_audio 是同一威胁模型：裸 IP 直链必须拦，DNS rebinding 残面与
    download_audio 同口径接受（host 字面校验，不解析 DNS）。"""
    from .media import _is_internal_host

    if not url.startswith(("http://", "https://")):
        return False
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        # 畸形 URL（如括号不配对的 IPv6 「http://[::1/x.jpg」）urlsplit 直接抛
        # ValueError——消费方（_fetch_bytes 逐跳预检、cards 构造、render 内联
        # 兜底）都没有对应兜底面，任一畸形封面就能炸掉整条链路。按「非公网」拒绝
        return False
    return not _is_internal_host(hostname)


async def palette_for(cover_url: str, session: aiohttp.ClientSession) -> dict | None:
    """取封面主色色板；失败返回 None（调用方回退品牌色）。"""
    if not cover_url or not cover_url.startswith("http"):
        return None
    async with _LOCK:
        hit = _CACHE.get(cover_url)
        if hit is not None and time.time() - hit[0] < _TTL:
            return hit[1]
        fut = _INFLIGHT.get(cover_url)
        if fut is None:
            fut = asyncio.ensure_future(_compute_tracked(cover_url, session))
            _INFLIGHT[cover_url] = fut
    # shield：单个等待者被取消时不要连带取消共享的 Future，否则其他人也拿不到结果。
    # 清理与写缓存都在 _compute_tracked 内部完成 —— 放调用方 finally 会在取消时二次抛
    # CancelledError，反而掩盖真实异常。
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        # close()（插件重载）取消共享 Future 时，所有等待者都会收到 CancelledError：
        # 它是 BaseException，穿出去会崩掉与关闭无关的渲染路径（render 的
        # except Exception 接不住）。等待者自身被取消时不吞——共享 Future 仍
        # pending（shield 保护着），据 done()/cancelled() 区分两种来源
        if fut.done() and fut.cancelled():
            return None
        raise


async def close() -> None:
    """取消在途取色/内联任务（插件重载/停用时调用）。

    在途任务挂在模块级 _INFLIGHT/_URI_INFLIGHT 上、不经 service.spawn，
    terminate 拿不到引用；不取消的话旧任务会拿着已关闭的 aiohttp 会话继续跑
    （异常被吞、无害但白耗）。
    """
    async with _LOCK:
        futs = [
            f
            for f in (*_INFLIGHT.values(), *_URI_INFLIGHT.values())
            if isinstance(f, asyncio.Task) and not f.done()
        ]
        _INFLIGHT.clear()
        _URI_INFLIGHT.clear()
    for task in futs:
        task.cancel()
    if futs:
        await asyncio.gather(*futs, return_exceptions=True)


async def _compute_tracked(url: str, session: aiohttp.ClientSession) -> dict | None:
    """执行取色并负责 in-flight 登记的清理与结果缓存。

    None（下载失败/非图片）不写缓存：瞬时网络抖动不该让主题色回退品牌默认色
    整整一个 TTL 窗口（对齐 _compute_cover_uri 的「失败不缓存」策略）；
    in-flight 合并仍保留，同 URL 的并发调用只算一次。"""
    try:
        palette = await _compute(url, session)
        if palette is not None:
            async with _LOCK:
                if len(_CACHE) >= _MAX:
                    for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: len(_CACHE) - _MAX + 1]:
                        _CACHE.pop(k, None)
                _CACHE[url] = (time.time(), palette)
        return palette
    finally:
        # 失败也要清：否则这个 URL 会永远命中同一个已异常的 Future
        async with _LOCK:
            if _INFLIGHT.get(url) is asyncio.current_task():
                _INFLIGHT.pop(url, None)


async def _compute(url: str, session: aiohttp.ClientSession) -> dict | None:
    try:
        raw = await _fetch_bytes(url, session)
        if not raw:
            return None
    except Exception:  # noqa: BLE001 — 封面下载失败只回退品牌色
        return None
    return await asyncio.to_thread(_palette_from_bytes, raw)


async def _fetch_bytes(url: str, session: aiohttp.ClientSession) -> bytes:
    """带逐跳内网复检的封面下载：手动跟随重定向（每跳过 _public_http_url），
    响应体循环 readany 读到 EOF、超限即断。取色与渲染内联（cover_data_uri）共用。

    aiohttp 默认自动跟随（最多 10 跳），跳数不归我们管时首跳预检挡不住
    「公网封面 URL 302 到内网」的绕行——渲染通道的 Chromium 同样自动跟随，
    唯有插件自己代理下载才能把复检落到每一跳。失败返回空 bytes。"""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not _public_http_url(current):
            return b""
        try:
            async with session.get(current, timeout=_DOWNLOAD_TIMEOUT, allow_redirects=False) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location", "")
                    if not loc:
                        return b""
                    current = str(resp.url.join(loc))
                    continue
                if resp.status != 200:
                    return b""
                # read(n) 缓冲区一有数据就返回（不凑满 n），大图会被截断；
                # 循环 readany 读到 EOF，超限即断（不把整张大图拉进内存）
                chunks: list[bytes] = []
                got = 0
                while True:
                    chunk = await resp.content.readany()
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > _MAX_BYTES:
                        return b""
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception:  # noqa: BLE001 — 网络失败按「无封面」处理
            return b""
    return b""


# 渲染内联（data URI）缓存：与取色缓存同 TTL，条目与单图字节双上限（base64 后
# 膨胀 1.33 倍，正常封面 ~200KB；32 张 × 2MB × 1.33 ≈ 85MB 是硬上限的最坏值，
# 正常远低于此）。in-flight 合并与取色路径同款：多群并发渲染同一张未命中封面
# 只发一次下载，不各自完整拉一遍
_URI_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()
_URI_INFLIGHT: dict[str, asyncio.Future] = {}
_URI_TTL = 600
_URI_MAX = 32
_URI_MAX_BYTES = 2 * 1024 * 1024


async def _cover_uri_tracked(url: str, session: aiohttp.ClientSession) -> str:
    """执行封面内联下载并负责 in-flight 登记的清理与结果缓存。"""
    try:
        uri = await _compute_cover_uri(url, session)
        if uri:
            async with _LOCK:
                while len(_URI_CACHE) >= _URI_MAX:
                    _URI_CACHE.popitem(last=False)
                _URI_CACHE[url] = (time.time(), uri)
        return uri
    finally:
        # 失败也要清：否则这个 URL 会永远命中同一个已异常的 Future
        async with _LOCK:
            if _URI_INFLIGHT.get(url) is asyncio.current_task():
                _URI_INFLIGHT.pop(url, None)


async def _compute_cover_uri(url: str, session: aiohttp.ClientSession) -> str:
    raw = await _fetch_bytes(url, session)
    if not raw or len(raw) > _URI_MAX_BYTES:
        # 失败/超限不缓存：瞬时网络抖动不该让封面缺席整个 TTL 窗口
        return ""
    mime = ""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:2] == b"\xff\xd8":
        mime = "image/jpeg"
    elif raw[:4] == b"GIF8":
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    if not mime:
        # 非图片（上游把 HTML 错误页当 200 返回等）：内联进 data URI 只会渲染出
        # 破图，按失败处理
        return ""
    import base64

    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


async def cover_data_uri(url: str, session: aiohttp.ClientSession) -> str:
    """封面 → ``data:image/...;base64`` URI（带缓存），失败返回空串。

    供渲染层把卡片里的图片 URL 内联：Chromium 对 ``<img src>``/hero 背景自动
    跟随重定向，公网封面 302 到内网可绕过构造层的字面预检。内联后 Chromium
    对封面零网络请求，渲染通道与下载/取色通道的威胁模型对齐。"""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    async with _LOCK:
        hit = _URI_CACHE.get(url)
        if hit is not None and time.time() - hit[0] < _URI_TTL:
            _URI_CACHE.move_to_end(url)
            return hit[1]
        fut = _URI_INFLIGHT.get(url)
        if fut is None:
            fut = asyncio.ensure_future(_cover_uri_tracked(url, session))
            _URI_INFLIGHT[url] = fut
    # shield 语义与 palette_for 相同：单个等待者被取消不连带取消共享 Future
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        if fut.done() and fut.cancelled():
            return ""
        raise


def _palette_from_bytes(raw: bytes) -> dict | None:
    """纯 CPU：解码图片 → 量化 → 选主色 → 生成色阶。任何异常回退 None。"""
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        # 解压炸弹预检：8MB PNG 可声明上亿像素，convert("RGB") 会真实分配约
        # 3 字节/像素（最大 ~500MB），且每个封面 URL 各触发一次。Pillow 默认
        # MAX_IMAGE_PIXELS 在 1~2 倍区间只告警不拦截，这里按尺寸硬拒
        if im.width * im.height > _MAX_PIXELS:
            return None
        im = im.convert("RGB")
        im = im.resize((48, 48))
        q = im.quantize(colors=6)
        pal = q.getpalette() or []
        counts = sorted(q.getcolors() or [], reverse=True)
        total = sum(n for n, _ in counts) or 1
        best: tuple[float, float, float] | None = None
        best_score = -1.0
        for n, idx in counts:
            if idx * 3 + 2 >= len(pal):
                continue
            r, g, b = pal[idx * 3], pal[idx * 3 + 1], pal[idx * 3 + 2]
            h, lig, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
            if s < 0.10 or lig < 0.06 or lig > 0.96:
                continue
            score = s * 1.2 - abs(lig - 0.5) * 0.9 + (n / total) * 0.5
            if score > best_score:
                best_score = score
                best = (h, lig, s)
        if best is None:
            return None
        return _build_ramp(best[0])
    except Exception:  # noqa: BLE001
        return None


def _hex(h: float, lig: float, s: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, min(max(lig, 0.0), 1.0), min(max(s, 0.0), 1.0))
    return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"


def _rgb_of(hex_str: str) -> tuple[int, int, int]:
    v = hex_str.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _rel_lum(rgb: tuple[int, int, int]) -> float:
    def f(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _rel_lum(_rgb_of(a)), _rel_lum(_rgb_of(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _build_ramp(h: float) -> dict:
    """由主色相生成整套色阶：所有前景色对浅底对比度 ≥ 4.5:1。"""
    soft = _hex(h, 0.955, 0.28)

    lig = 0.46
    while _contrast(_hex(h, lig, 0.60), soft) < 4.5 and lig > 0.28:
        lig -= 0.015
    accent = _hex(h, lig, 0.60)

    ml = 0.40
    while _contrast(_hex(h, ml, 0.16), soft) < 4.5 and ml > 0.22:
        ml -= 0.015
    muted = _hex(h, ml, 0.16)

    r, g, b = _rgb_of(accent)
    return {
        "nm": accent,
        "nm_deep": _hex(h, max(lig - 0.10, 0.22), 0.62),
        "hero_a": _hex(h, min(lig + 0.10, 0.60), 0.62),
        "hero_b": _hex(h, max(lig - 0.12, 0.26), 0.68),
        "card_a": _hex(h, 0.985, 0.12),
        "card_b": _hex(h, 0.945, 0.22),
        "soft": soft,
        "muted": muted,
        "text": _hex(h, 0.16, 0.28),
        "chip": f"rgba({r}, {g}, {b}, 0.09)",
        "line": f"rgba({r}, {g}, {b}, 0.16)",
    }
