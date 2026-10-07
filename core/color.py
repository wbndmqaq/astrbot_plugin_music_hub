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
    return await asyncio.shield(fut)


async def _compute_tracked(url: str, session: aiohttp.ClientSession) -> dict | None:
    """执行取色并负责 in-flight 登记的清理与结果缓存。"""
    try:
        palette = await _compute(url, session)
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
        async with session.get(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            raw = await resp.read()
        if not raw or len(raw) > _MAX_BYTES:
            return None
    except Exception:  # noqa: BLE001 — 封面下载失败只回退品牌色
        return None
    return await asyncio.to_thread(_palette_from_bytes, raw)


def _palette_from_bytes(raw: bytes) -> dict | None:
    """纯 CPU：解码图片 → 量化 → 选主色 → 生成色阶。任何异常回退 None。"""
    try:
        import io

        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
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
