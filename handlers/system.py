"""系统路由：帮助 / 设置 / 音质 / API / 统计 / 测试 / WebUI / 开关。"""

from __future__ import annotations

import re

from ..core import SOURCE_NAMES, SOURCES
from ..core.errors import ApiError
from ..core.quality import KG_LABEL, NCM_LABEL, QQ_LABEL
from .base import Route

_SRC = r"(ncm|kg|qqm|qq)?"
_SRC_STRICT = r"(ncm|kg|qqm|qq)"

_RE_HELP = r"^\s*#?(?:音乐|mh|点歌)?\s*(?:帮助|菜单|help)\s*$"
_RE_SETTINGS = r"^\s*#?(?:音乐|mh)?\s*(?:设置|配置)\s*$"
_RE_QUALITY = rf"^\s*#?{_SRC}\s*音质\s*(\S*)\s*$"
_RE_API = rf"^\s*#?{_SRC_STRICT}\s*api\s+(\S+)\s*$"
_RE_STATS = r"^\s*#?(?:音乐|mh)?\s*统计\s*$"
_RE_TEST = r"^\s*#?(?:音乐|mh)?\s*测试\s*$"
_RE_WEBUI = r"^\s*#?(?:音乐|mh)?\s*web\s*ui\s*$"
_RE_TOGGLE = r"^\s*#?(?:音乐|mh)?\s*(开启|关闭)\s+(点歌|解析|卡片|语音|文件)\s*$"

_QUALITY_TABLES = {"ncm": NCM_LABEL, "kg": KG_LABEL, "qq": QQ_LABEL}


def _src_of(token: str | None, default: str = "") -> str:
    from ..core.sources import token_to_source

    return token_to_source(token, default)


async def run_help(service, event):
    data = service.help_data()
    await service.reply_card_or_text(event, data, "help", "auto", _format_help_text)


def _format_help_text(data: dict) -> str:
    lines = [f"♪ Music Hub v{data.get('version', '')} 聚合点歌"]
    for sec in data.get("sections", []):
        lines.append(f"▎{sec.get('title', '')}")
        for it in sec.get("items", []):
            lines.append(f"  {it.get('name', '')} — {it.get('desc', '')}")
    lines.append(data.get("tip", ""))
    return "\n".join(lines)


async def run_settings(service, event):
    data = service.settings_data()
    await service.reply_card_or_text(event, data, "settings", "auto", _format_settings_text)


def _format_settings_text(data: dict) -> str:
    lines = ["♪ Music Hub 设置"]
    for s in data.get("sources", []):
        state = "✓" if s.get("enabled") else "✗"
        login = "已登录" if s.get("logged") else "未登录"
        lines.append(
            f"· {s.get('name', '')} {state} · {s.get('apiBase', '')} · 音质 {s.get('quality', '')} · {login}"
        )
    lines.append(f"默认音源：{data.get('defaultSource', '')} · 列表数：{data.get('maxList', '')}")
    for t in data.get("toggles", []):
        lines.append(f"· {t.get('name', '')}：{'开' if t.get('on') else '关'}")
    if data.get("webui", {}).get("enabled"):
        lines.append(f"WebUI：端口 {data['webui']['port']}")
    return "\n".join(lines)


async def run_quality(service, event):
    m = re.search(_RE_QUALITY, event.message_str, re.IGNORECASE)
    src = _src_of(m.group(1) if m else None, service.config.default_source)
    if src == "auto":
        src = "ncm"
    value = (m.group(2) if m else "").strip().lower()
    if not value:
        cur = service.config.src_quality(src)
        await service.reply(
            event, f"[{SOURCE_NAMES[src]}] 当前最高音质：{cur}（{_QUALITY_TABLES[src].get(cur, cur)}）"
        )
        return
    if value not in _QUALITY_TABLES[src]:
        options = "/".join(_QUALITY_TABLES[src].keys())
        await service.reply(event, f"[{SOURCE_NAMES[src]}] 不支持的音质档位，可选：{options}")
        return
    node = dict(service.config.src_node(src))
    node["quality"] = value
    service.config.set(src, node)
    saved = await service.config.save_async()
    await service.reply(
        event,
        f"[{SOURCE_NAMES[src]}] 最高音质已设为 {value}（{_QUALITY_TABLES[src].get(value, value)}）"
        + ("" if saved else "（保存失败，重启后失效）"),
    )


async def run_api(service, event):
    m = re.search(_RE_API, event.message_str, re.IGNORECASE)
    src = _src_of(m.group(1) if m else None)
    url = (m.group(2) if m else "").strip()
    if src in ("", "qq"):
        await service.reply(event, "用法：ncm api <地址> 或 kg api <地址>（QQ 音源内置，无需配置）")
        return
    from ..core.config import normalize_base

    url = normalize_base(url)
    node = dict(service.config.src_node(src))
    node["apiBase"] = url
    service.config.set(src, node)
    saved = await service.config.save_async()
    await service.reply(event, f"[{SOURCE_NAMES[src]}] API 地址已设置并测试中…")
    try:
        if src == "ncm":
            await service.ncm.request("/song/url/v1", {"id": 1, "level": "standard"})
        else:
            await service.kg.request("/search/hot", {})
        await service.reply(event, "连通性 OK ✓" + ("" if saved else "（保存失败，重启后失效）"))
    except ApiError as e:
        await service.reply(event, f"连通性失败：{e.user_msg()}")


async def run_stats(service, event):
    snap = service.stats.snapshot(days=7)
    lines = ["♪ Music Hub 调用统计"]
    lines.append(f"今日：{snap.get('todayTotal', 0)} 次 · 累计：{snap.get('totalAll', 0)} 次")
    for src in SOURCES:
        t = snap.get("today", {}).get(src, 0)
        tt = snap.get("total", {}).get(src, 0)
        lines.append(f"· {SOURCE_NAMES[src]}：今日 {t} / 累计 {tt}")
    lines.append("WebUI 可查看 14 天图表与明细")
    await service.reply(event, "\n".join(lines))


async def run_test(service, event):
    lines = ["♪ Music Hub 连通性测试"]
    # QQ
    try:
        from ..core.api.qq import available as qq_ok

        if qq_ok():
            status = await service.qq.login_status()
            lines.append(f"· QQ音乐：内置库 ✓（{'已登录' if status.get('loggedIn') else '匿名'}）")
        else:
            lines.append("· QQ音乐：✗ 未安装 qqmusic-api-python")
    except Exception as e:  # noqa: BLE001
        lines.append(f"· QQ音乐：✗ {e}")
    # ncm
    if not service.config.src_api_base("ncm"):
        lines.append("· 网易云：未配置 API")
    else:
        try:
            await service.ncm.search("测试", 1)
            lines.append(f"· 网易云：✓（{service.config.src_api_base('ncm')}）")
        except Exception as e:  # noqa: BLE001
            lines.append(f"· 网易云：✗ {e}")
    # kg
    if not service.config.src_api_base("kg"):
        lines.append("· 酷狗：未配置 API")
    else:
        try:
            await service.kg.request("/search/hot", {})
            lines.append(f"· 酷狗：✓（{service.config.src_api_base('kg')}）")
        except Exception as e:  # noqa: BLE001
            lines.append(f"· 酷狗：✗ {e}")
    await service.reply(event, "\n".join(lines))


async def run_webui(service, event):
    cfg = service.config
    if not cfg.webui_enable:
        await service.reply(event, "WebUI 未启用（插件配置里开启）")
        return
    host = cfg.webui_host
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.5.5.5", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:  # noqa: BLE001
        lan_ip = "127.0.0.1"
    display = lan_ip if host in ("0.0.0.0", "") else host
    await service.reply(
        event,
        f"♪ Music Hub 管理面板：http://{display}:{cfg.webui_port}\n（默认密码见插件配置 webui.password）",
    )


async def run_toggle(service, event):
    m = re.search(_RE_TOGGLE, event.message_str)
    on = (m.group(1) if m else "") == "开启"
    target = m.group(2) if m else ""
    key = {
        "点歌": "enableSongRequest",
        "解析": "enableResolve",
        "卡片": "renderListCard",
        "语音": "sendVocal",
        "文件": "uploadFile",
    }.get(target)
    if not key:
        return
    service.config.set(key, on)
    saved = await service.config.save_async()
    await service.reply(
        event, f"已{'开启' if on else '关闭'}{target}" + ("" if saved else "（保存失败，重启后失效）")
    )


def routes() -> list[Route]:
    return [
        Route(re.compile(_RE_HELP, re.IGNORECASE), "mh_help", "帮助", run_help, priority=6),
        Route(re.compile(_RE_SETTINGS, re.IGNORECASE), "mh_settings", "查看设置", run_settings, priority=6),
        Route(
            re.compile(_RE_QUALITY, re.IGNORECASE),
            "mh_quality",
            "设置音质",
            run_quality,
            admin=True,
            priority=6,
        ),
        Route(re.compile(_RE_API, re.IGNORECASE), "mh_api", "设置 API 地址", run_api, admin=True, priority=6),
        Route(re.compile(_RE_STATS, re.IGNORECASE), "mh_stats", "调用统计", run_stats, priority=6),
        Route(re.compile(_RE_TEST, re.IGNORECASE), "mh_test", "连通性测试", run_test, admin=True, priority=6),
        Route(re.compile(_RE_WEBUI, re.IGNORECASE), "mh_webui", "WebUI 地址", run_webui, priority=6),
        Route(re.compile(_RE_TOGGLE), "mh_toggle", "功能开关", run_toggle, admin=True, priority=6),
    ]
