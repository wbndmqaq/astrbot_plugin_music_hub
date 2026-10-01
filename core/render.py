"""HTML 卡片渲染引擎：Jinja2 模板 + 常驻 Playwright Chromium。

- 模板位于 ``resources/html/<name>/<name>.html``，编译缓存
- 封面取色动态主题注入（``data["theme"]`` → CSS 变量覆盖）
- 截图 ``animations="disabled"``：入场动画直接跳到末态，不会截到半程
- 启动失败短路：渲染环境缺失是稳定状态，后续渲染即时回退纯文本
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from astrbot.api import logger

from . import color as mh_color

_CHROME_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--font-render-hinting=none",
    "--enable-font-antialiasing",
    "--hide-scrollbars",
]

_playwright = None
_browser = None
_lock = asyncio.Lock()
_launch_failed = False
_hint_logged = False

_RENDER_TUTORIAL = (
    "本插件不自动执行任何系统级安装，请按以下步骤手动操作：\n"
    "① pip install playwright\n"
    "② python -m playwright install chromium\n"
    "③ Linux 容器缺系统库时：python -m playwright install-deps chromium\n"
    "完成后重载本插件即可渲染图片；未安装期间所有指令自动回退纯文本，点歌播放不受影响。"
)

TAG = "[music_hub]"


def _log_env_hint(reason: str) -> None:
    global _hint_logged
    if _hint_logged:
        logger.warning(f"{TAG} 卡片渲染不可用（{reason}），已回退纯文本")
        return
    _hint_logged = True
    logger.error(f"{TAG} 卡片渲染不可用（{reason}）。\n{_RENDER_TUTORIAL}")


async def _get_browser():
    global _playwright, _browser, _launch_failed
    if _browser is not None:
        return _browser
    if _launch_failed:
        return None
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        _launch_failed = True
        _log_env_hint(f"缺少依赖 {e.name}")
        return None
    async with _lock:
        if _browser is not None:
            return _browser
        pw = None
        try:
            pw = await async_playwright().start()
            _browser = await pw.chromium.launch(headless=True, args=_CHROME_ARGS)
            _playwright = pw
        except Exception as e:  # noqa: BLE001
            _launch_failed = True
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                _log_env_hint("未下载 Chromium")
            elif "shared libraries" in msg or "shared object" in msg:
                _log_env_hint("Chromium 缺少系统运行库")
            else:
                logger.error(f"{TAG} Chromium 启动失败: {e}")
            if pw is not None:
                with contextlib.suppress(Exception):
                    await pw.stop()
            return None
    return _browser


def _remove_output(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


# ──────────── 模板 ────────────
_compiled_templates: dict[str, object] = {}


def _compile_template(tmpl_path: str):
    import jinja2

    with open(tmpl_path, encoding="utf-8") as f:
        src = f.read()
    return jinja2.Template(src, autoescape=True)


async def load_template(tmpl_path: str):
    template = _compiled_templates.get(tmpl_path)
    if template is None:
        template = await asyncio.to_thread(_compile_template, tmpl_path)
        _compiled_templates[tmpl_path] = template
    return template


_DICT_METHODS = frozenset(
    {"items", "keys", "values", "get", "copy", "update", "pop", "popitem", "clear", "setdefault"}
)


class _SafeData(dict):
    """缺失键给空值；与字典方法同名键优先当数据键。"""

    def __missing__(self, key):
        return ""

    def __getattribute__(self, key):
        if key in _DICT_METHODS:
            try:
                return dict.__getitem__(self, key)
            except KeyError:
                return ""
        return dict.__getattribute__(self, key)


def wrap_card_data(value):
    if isinstance(value, dict):
        return _SafeData({k: wrap_card_data(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap_card_data(v) for v in value]
    if isinstance(value, tuple):
        return tuple(wrap_card_data(v) for v in value)
    return value


# ──────────── 主题 ────────────
_THEME_VARS = (
    "nm",
    "nm_deep",
    "hero_a",
    "hero_b",
    "card_a",
    "card_b",
    "soft",
    "muted",
    "text",
    "chip",
    "line",
)
_HERO_BG_CARDS = ("detail", "lyric", "comment")

DEFAULT_PAGE_BG = "#faf4f2"


def _find_cover(data: dict) -> str:
    try:
        c = data.get("cover")
        if isinstance(c, str) and c.startswith("http"):
            return c
        for list_key in ("songs", "items"):
            lst = data.get(list_key)
            if isinstance(lst, list) and lst and isinstance(lst[0], dict):
                c = lst[0].get("cover")
                if isinstance(c, str) and c.startswith("http"):
                    return c
    except Exception:  # noqa: BLE001
        return ""
    return ""


async def apply_theme(data: dict) -> None:
    """发现封面 → 取色（带缓存）→ 写入 data["theme"]；任何失败静默回退品牌色。"""
    cover = _find_cover(data)
    if not cover:
        return
    try:
        from .api.http import get_session

        palette = await mh_color.palette_for(cover, get_session())
        if palette:
            palette["coverUrl"] = cover
            data["theme"] = palette
    except Exception:  # noqa: BLE001
        pass


def theme_bg(data: dict, source: str) -> str:
    theme = data.get("theme") if isinstance(data, dict) else None
    soft = theme.get("soft") if isinstance(theme, dict) else None
    if isinstance(soft, str) and soft.startswith("#") and len(soft) == 7:
        return soft
    return mh_color.brand_for(source).get("soft", DEFAULT_PAGE_BG)


def _hero_bg_css(url: str) -> str:
    safe = url.replace("\\", "").replace('"', "").replace("'", "")
    if not safe.startswith("http"):
        return ""
    return (
        ".hero{isolation:isolate;position:relative;overflow:hidden;}"
        f'.hero::before{{content:"";position:absolute;inset:0;z-index:-1;'
        f'background:url("{safe}") center/cover no-repeat;'
        f"filter:blur(22px) saturate(1.25) brightness(.9);transform:scale(1.2);}}"
    )


def inject_theme_css(html: str, data: dict, tmpl_name: str, source: str) -> str:
    """基础增强 + 音源品牌色 / 封面取色主题注入首个 </style> 前。"""
    css = (
        "*{font-variant-numeric:tabular-nums;}"
        'body{font-family:"MiSans","HarmonyOS Sans SC","Microsoft YaHei UI",'
        '"Microsoft YaHei","PingFang SC","Noto Sans SC",sans-serif;}'
    )
    pairs = []
    if source and source in mh_color.BRANDS:
        brand = mh_color.brand_for(source)
        pairs.extend(f"--mh-{k.replace('_', '-')}:{v}" for k, v in brand.items() if v)
    theme = data.get("theme")
    if isinstance(theme, dict):
        pairs.extend(
            f"--mh-{k.replace('_', '-')}:{str(theme[k]).replace(';', '')}"
            for k in _THEME_VARS
            if theme.get(k)
        )
        cover = theme.get("coverUrl")
        if cover and any(k in tmpl_name.lower() for k in _HERO_BG_CARDS):
            css += _hero_bg_css(str(cover))
    if pairs:
        css += f":root{{{';'.join(pairs)};}}"
    if "</style>" not in html:
        return html
    return html.replace("</style>", css + "</style>", 1)


# ──────────── 渲染 ────────────
async def render_html_to_png(html: str, out_path: str, bg: str = DEFAULT_PAGE_BG) -> bool:
    browser = await _get_browser()
    if browser is None:
        return False
    page = await browser.new_page(viewport={"width": 640, "height": 2200}, device_scale_factor=3)
    try:
        try:
            await page.set_content(html, wait_until="networkidle", timeout=12000)
        except Exception:  # noqa: BLE001
            with contextlib.suppress(Exception):
                await page.set_content(html, wait_until="load", timeout=8000)
        safe_bg = bg if isinstance(bg, str) and bg.startswith("#") and len(bg) == 7 else DEFAULT_PAGE_BG
        await page.evaluate(
            """(bg) => {
                document.documentElement.style.background = bg;
                document.body.style.background = bg;
                document.documentElement.style.width = 'fit-content';
                document.body.style.width = 'fit-content';
                document.documentElement.style.margin = '0';
                document.body.style.margin = '0';
            }""",
            safe_bg,
        )
        with contextlib.suppress(Exception):
            await page.evaluate("document.fonts.ready.then(() => {})")
        await page.wait_for_timeout(200)

        el = await page.query_selector(".page") or await page.query_selector(".card") or page
        box = await el.bounding_box()
        if box:
            need_w = int(box["x"] + box["width"] + 4)
            need_h = int(box["y"] + box["height"] + 4)
            cur = page.viewport_size
            if need_w > cur["width"] or need_h > cur["height"]:
                await page.set_viewport_size(
                    {"width": max(cur["width"], need_w), "height": max(cur["height"], need_h)}
                )
                await page.wait_for_timeout(80)
        await el.screenshot(path=out_path, type="png", omit_background=False, animations="disabled")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"{TAG} 渲染失败: {e}")
        _remove_output(out_path)
        return False
    except BaseException:
        _remove_output(out_path)
        raise
    finally:
        with contextlib.suppress(Exception):
            await page.close()


async def close() -> None:
    global _playwright, _browser
    if _browser is not None:
        with contextlib.suppress(Exception):
            await _browser.close()
        _browser = None
    if _playwright is not None:
        with contextlib.suppress(Exception):
            await _playwright.stop()
        _playwright = None


def template_path(tmpl_root: Path, name: str) -> str:
    return str(tmpl_root / name / f"{name}.html")
