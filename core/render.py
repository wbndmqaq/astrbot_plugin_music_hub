"""HTML 卡片渲染引擎：Jinja2 模板 + Playwright Chromium。

设计要点：

- 浏览器运行时收敛为 :class:`Renderer` 实例，随 service 创建/销毁。
  启动失败的状态在 close() 时归位，插件重载后可重新尝试启动，
  不会因一次失败永久短路（模块级失败标记就无法恢复）。
- 模板编译缓存仍为模块级（跨实例复用，模板内容不变时无需重编译）。
- 截图 ``animations="disabled"``：入场动画直接跳到末态，不会截到半程。
- 渲染环境缺失属稳定状态，失败后即时回退纯文本，不影响点歌。
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


# ──────────── 模板（跨实例共享，模板内容不变时无需重编译） ────────────
_compiled_templates: dict[str, object] = {}


def _compile_template(tmpl_path: str):
    import jinja2

    with open(tmpl_path, encoding="utf-8") as f:
        src = f.read()
    # ChainableUndefined：缺失键返回自身（falsy），任意属性/索引访问都不报错。
    # 卡片模板大量使用 {{ a.b.c }} 形式的可选字段，直接用 Jinja 原生能力兜底。
    return jinja2.Template(
        src,
        autoescape=True,
        undefined=jinja2.ChainableUndefined,
    )


async def load_template(tmpl_path: str):
    template = _compiled_templates.get(tmpl_path)
    if template is None:
        template = await asyncio.to_thread(_compile_template, tmpl_path)
        _compiled_templates[tmpl_path] = template
    return template


# ──────────── 浏览器运行时（随实例生命周期） ────────────
class Renderer:
    """常驻 Chromium 实例。close() 后状态归位，允许再次尝试启动。"""

    def __init__(self):
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._disabled = False

    @property
    def disabled(self) -> bool:
        """环境不可用（如未安装 Playwright）时为 True，调用方应直接回退纯文本。"""
        return self._disabled

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        if self._disabled:
            return None
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            self._disabled = True
            _log_env_hint(f"缺少依赖 {e.name}")
            return None
        async with self._lock:
            if self._browser is not None:
                return self._browser
            pw = None
            try:
                pw = await async_playwright().start()
                self._browser = await pw.chromium.launch(headless=True, args=_CHROME_ARGS)
                self._pw = pw
            except Exception as e:  # noqa: BLE001
                self._disabled = True
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
        return self._browser

    async def render(self, html: str, out_path: str, bg: str) -> bool:
        """渲染 HTML → PNG。成功返回 True，失败返回 False（调用方回退纯文本）。"""
        browser = await self._ensure_browser()
        if browser is None:
            return False
        page = await browser.new_page(viewport={"width": 640, "height": 2200}, device_scale_factor=3)
        try:
            try:
                await page.set_content(html, wait_until="networkidle", timeout=12000)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    await page.set_content(html, wait_until="load", timeout=8000)
            safe_bg = bg if isinstance(bg, str) and bg.startswith("#") and len(bg) == 7 else "#faf4f2"
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

    async def close(self) -> None:
        """关闭浏览器并归位状态（disabled 复位，允许下次重载重试启动）。"""
        if self._browser is not None:
            with contextlib.suppress(Exception):
                await self._browser.close()
            self._browser = None
        if self._pw is not None:
            with contextlib.suppress(Exception):
                await self._pw.stop()
            self._pw = None
        self._disabled = False


def _remove_output(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


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


def template_path(tmpl_root: Path, name: str) -> str:
    return str(tmpl_root / name / f"{name}.html")
