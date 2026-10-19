"""系统路由：帮助 / 设置 / 音质 / API / 统计 / 测试 / WebUI / 开关。"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..core import SOURCE_NAMES, SOURCE_QQ, SOURCES
from ..core.errors import ApiError
from ..core.quality import KG_LABEL, NCM_LABEL, QQ_LABEL
from ..core.sources import token_to_source
from .base import Route

_SRC = r"(ncm|kg|qqm|qq)?"
_SRC_STRICT = r"(ncm|kg|qqm|qq)"

_RE_HELP = r"^\s*[#＃]?(?:音乐|mh|点歌)?\s*(?:帮助|菜单|help)\s*$"
_RE_SETTINGS = r"^\s*[#＃]?(?:音乐|mh)?\s*(?:设置|配置)\s*$"
# 音源可在「音质」前（ncm 音质 320）也可在后（音质 ncm 320，兜底提示用的是这种语序），
# 两种都要能匹配，否则照提示输入会静默无响应。
# 后语序分支放前面：否则「音质 ncm」会被前语序分支先命中、把 ncm 当成档位值。
# _SRC 自带捕获组：组 1 = 前语序音源，组 2 = 后语序音源，组 3 = 档位值
_RE_QUALITY = rf"^\s*[#＃]?(?:音质\s*(?:{_SRC}\s*)?|{_SRC}\s*音质)\s*(\S*)\s*$"
# 地址组可选：裸「ncm api」（漏打地址）原先整条不匹配、静默无响应，run_api
# 的用法分支成了死代码——与「换源」「登录」的修法对齐，空地址由 handler 回用法
_RE_API = rf"^\s*[#＃]?{_SRC_STRICT}\s*api(?:\s+(.+?))?\s*$"
_RE_STATS = r"^\s*[#＃]?(?:音乐|mh)?\s*统计\s*$"
_RE_TEST = r"^\s*[#＃]?(?:音乐|mh)?\s*测试\s*$"
_RE_WEBUI = r"^\s*[#＃]?(?:音乐|mh)?\s*web\s*ui\s*$"
_RE_TOGGLE = r"^\s*[#＃]?(?:音乐|mh)?\s*(开启|关闭)\s*(点歌|解析|卡片|语音|文件)\s*$"

_QUALITY_TABLES = {"ncm": NCM_LABEL, "kg": KG_LABEL, "qq": QQ_LABEL}

# 「设置 API 地址」后的连通性探针：source → (请求路径, 参数)。
# 只含走外部 HTTP API 的平台——QQ 是进程内内置库，不需要也不该探 apiBase。
API_PROBE_PARAMS: dict[str, tuple[str, dict]] = {
    "ncm": ("/song/url/v1", {"id": 1, "level": "standard"}),
    "kg": ("/search/hot", {}),
}

# 「测试」指令的连通性探针：source → (展示用短名, 探针调用)。
# 短名沿用各平台惯用叫法（网易云/酷狗，而非 SOURCE_NAMES 的「网易云音乐」），
# 因为这些字符串是用户可见输出，改名会变更既有文案。
# 两家的探针调用也不同（网易云用 search、酷狗用裸 request），故存 callable 而非路径。
CONNECTIVITY_PROBES: dict[str, tuple[str, Any]] = {
    "ncm": ("网易云", lambda client: client.search("测试", 1)),
    "kg": ("酷狗", lambda client: client.request("/search/hot", {})),
}


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
    # 组 1 =「音质」前语序的音源，组 2 = 后语序的音源，组 3 = 档位值
    src_token = (m.group(1) or m.group(2)) if m else None
    explicit = bool(src_token)
    src = token_to_source(src_token, service.config.default_source)
    if src == "auto":
        src = "ncm"
    value = (m.group(3) if m else "").strip().lower()
    if not value:
        cur = service.config.src_quality(src)
        # 默认音源是 auto 时未写前缀会静默落到 ncm：提示指定方式，避免改错源
        implicit_tip = (
            "" if explicit or service.config.default_source != "auto" else "；可用「音质 ncm 320」指定音源"
        )
        await service.reply(
            event,
            f"[{SOURCE_NAMES[src]}] 当前最高音质：{cur}（{_QUALITY_TABLES[src].get(cur, cur)}）{implicit_tip}",
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
    src = token_to_source(m.group(1) if m else None)
    url = (m.group(2) if m else "").strip()
    if src in ("", "qq"):
        await service.reply(event, "用法：ncm api <地址> 或 kg api <地址>（QQ 音源内置，无需配置）")
        return
    if not url:
        # 地址组改可选后的空参形态：回用法而不是静默（备注校验对空串恒假，须前置）
        await service.reply(event, f"用法：{src} api <地址>")
        return
    if re.search(r"\s", url):
        # 正则放宽后整段（含备注）都会被取进地址组：地址本身不能含空白，
        # 这里拦下给用法反馈，而不是把脏地址写进配置
        await service.reply(event, "API 地址不能包含空格")
        return
    from ..core.config import normalize_base

    url = normalize_base(url)
    # 只放行 http/https：normalize_base 不校验协议，file://、ftp:// 等会被
    # 服务器直接发起请求（admin-only，但 WebUI 弱口令场景下面会被放大）
    if not url.lower().startswith(("http://", "https://")):
        await service.reply(event, "API 地址必须以 http:// 或 https:// 开头")
        return
    # 拒绝内网/回环地址（与 WebUI 写侧 _api_admin 同口径）：持有管理员会话者
    # 可借「测试」的连通性输出差异对内网做存活探测
    from urllib.parse import urlparse

    from ..core.media import _is_internal_host

    if _is_internal_host(urlparse(url).hostname or ""):
        await service.reply(
            event,
            "拒绝内网/回环地址：服务器会向该地址发起请求，"
            "填公网可访问的 API 服务地址（本机自建请用反向代理）",
        )
        return
    node = dict(service.config.src_node(src))
    old_base = node.get("apiBase", "")
    node["apiBase"] = url
    service.config.set(src, node)
    saved = await service.config.save_async()
    await service.reply(event, f"[{SOURCE_NAMES[src]}] API 地址已设置并测试中…")
    try:
        # 平台独占能力：连通性探针路径因平台而异（网易云试取播放地址、酷狗试热搜），
        # 且 QQ 内置库无需配置 api（上面已拦下），故按平台分支选探针。
        path, params = API_PROBE_PARAMS[src]
        await service.client_of(src).request(path, params)
        await service.reply(event, "连通性 OK ✓" + ("" if saved else "（保存失败，重启后失效）"))
    except asyncio.CancelledError:
        # 重载/停用的取消同样不能把坏地址留在磁盘：回滚后原样上抛（取消语义不变，
        # 由路由兜底的 CancelledError 通道收尾）
        node["apiBase"] = old_base
        service.config.set(src, node)
        await service.config.save_async()
        raise
    except ApiError as e:
        # 探针走的就是配置里的新地址，只能先保存再探测；失败回滚旧值，别让坏地址留在
        # 配置里（客户端按 config 现读 base，set 即生效，回滚无需重建客户端）。
        node["apiBase"] = old_base
        service.config.set(src, node)
        restored = await service.config.save_async()
        if restored:
            await service.reply(event, f"新地址连通性失败：{e.user_msg()}，已还原为原地址")
        else:
            # 还原保存失败：坏地址仍在磁盘、重启后依旧生效——文案必须说清这个状态，
            # 「已还原（保存失败，重启后失效）」字面意思恰好说反
            await service.reply(
                event,
                f"新地址连通性失败：{e.user_msg()}。还原未能保存，重启后仍为新地址，请手动改回或重试",
            )
    except Exception as e:  # noqa: BLE001
        # http.request_json 对非 "closed" 的 RuntimeError 等意外异常是显式 re-raise
        # 的（探针段的 CancelledError/ApiError 兜不住）：漏接会既不回滚也不告知，
        # 坏地址留在配置里且管理员只看到「执行失败」。与 ApiError 分支同一回滚口径，
        # 异常原文落日志不透出（service.log_warn 已做脱敏）
        service.log_warn(f"[music_hub] API 连通性探针异常: {e}")
        node["apiBase"] = old_base
        service.config.set(src, node)
        restored = await service.config.save_async()
        await service.reply(
            event,
            "新地址连通性失败（服务异常），已还原为原地址"
            if restored
            else "新地址连通性失败，且还原未能保存，请手动改回",
        )


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
    # QQ：内置库，无需配置 API，故单独一段（也用它自己的 available() 探测）
    try:
        from ..core.api.qq import available as qq_ok

        if qq_ok():
            status = await service.client_of(SOURCE_QQ).login_status()
            lines.append(f"· QQ音乐：内置库 ✓（{'已登录' if status.get('loggedIn') else '匿名'}）")
        else:
            lines.append("· QQ音乐：✗ 未安装 qqmusic-api-python")
    except ApiError as e:
        lines.append(f"· QQ音乐：✗ {e.user_msg()}")
    except Exception as e:  # noqa: BLE001
        service.log_warn(f"连通性测试(QQ)失败: {e}")
        lines.append(f"· QQ音乐：✗ {type(e).__name__}")
    # 走 HTTP API 的平台：探针调用因平台而异，按 CONNECTIVITY_PROBES 查表后统一遍历，
    # 新增平台只需加一行探针，无需再复制一段 ncm/kg 分支。
    # label 用各平台惯用短名（不是 SOURCE_NAMES 的「网易云音乐」），与既有输出一致。
    for src, probe in CONNECTIVITY_PROBES.items():
        label, fetch = probe
        base = service.config.src_api_base(src)
        if not base:
            lines.append(f"· {label}：未配置 API")
            continue
        try:
            await fetch(service.client_of(src))
            lines.append(f"· {label}：✓（{base}）")
        except ApiError as e:
            lines.append(f"· {label}：✗ {e.user_msg()}")
        except Exception as e:  # noqa: BLE001
            # 原始异常可能带 host:port 等部署细节，只落日志；用户侧给错误类别
            service.log_warn(f"连通性测试({label})失败: {e}")
            lines.append(f"· {label}：✗ {type(e).__name__}")
    await service.reply(event, "\n".join(lines))


# UDP connect 探测用的对端地址： AliDNS anycast（纯路由探测，不发包不依赖其 DNS 服务）。
# 只为让 getsockname 吐出本机出口 IP，选任何稳定公网地址皆可；失败回环兜底
_LAN_PROBE_ADDR = ("223.5.5.5", 80)


def _detect_lan_ip() -> str:
    """探测本机局域网 IP。UDP connect 不发包，通常瞬时返回；失败回环地址。"""
    import socket

    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(_LAN_PROBE_ADDR)
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        if s is not None:
            s.close()


async def run_webui(service, event):
    cfg = service.config
    if not cfg.webui_enable:
        await service.reply(event, "WebUI 未启用（插件配置里开启）")
        return
    host = cfg.webui_host
    lan_ip = await asyncio.to_thread(_detect_lan_ip)  # socket 探测放线程池
    display = lan_ip if host in ("0.0.0.0", "") else host
    await service.reply(
        event,
        # 配置里存的是 PBKDF2 哈希（登录用不了）：初始口令在插件数据目录的
        # 告知文件里，登录改密后该文件自动删除
        f"♪ Music Hub 管理面板：http://{display}:{cfg.webui_port}\n"
        "（初始密码见插件数据目录 webui_initial_password.txt，登录后请修改密码）",
    )


async def run_toggle(service, event):
    # 与路由 filter 同 flag：大写前缀（如「MH 开启 点歌」）过路由后体内也要能匹配上
    m = re.search(_RE_TOGGLE, event.message_str, re.IGNORECASE)
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
        # 正则限定了开关词，正常到不了这里；配置漂移（新增开关忘同步映射）时
        # 给一句反馈而不是静默无响应
        await service.reply(event, "未知开关，可用：点歌 / 解析 / 卡片 / 语音 / 文件")
        return
    service.config.set(key, on)
    saved = await service.config.save_async()
    await service.reply(
        event, f"已{'开启' if on else '关闭'}{target}" + ("" if saved else "（保存失败，重启后失效）")
    )


def routes() -> list[Route]:
    return [
        Route(re.compile(_RE_HELP, re.IGNORECASE), "mh_help", "帮助", run_help, priority=6),
        # 设置 / 统计 / WebUI 三条会输出 apiBase、端口等部署与账号信息，
        # 与 auth.py「状态」同门槛（同 mh_status 的 admin 理由）
        Route(
            re.compile(_RE_SETTINGS, re.IGNORECASE),
            "mh_settings",
            "查看设置（管理员）",
            run_settings,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_QUALITY, re.IGNORECASE),
            "mh_quality",
            "设置音质（管理员）",
            run_quality,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_API, re.IGNORECASE),
            "mh_api",
            "设置 API 地址（管理员）",
            run_api,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_STATS, re.IGNORECASE),
            "mh_stats",
            "调用统计（管理员）",
            run_stats,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_TEST, re.IGNORECASE),
            "mh_test",
            "连通性测试（管理员）",
            run_test,
            admin=True,
            priority=6,
            # 每次全量探针最多 5 个上游请求（ncm search + kg 热搜 + QQ 登录态）：
            # 与其他 admin 路由同口径挂 2s 轻频控，防连发刷穿上游
            throttle=2.0,
        ),
        Route(
            re.compile(_RE_WEBUI, re.IGNORECASE),
            "mh_webui",
            "WebUI 地址（管理员）",
            run_webui,
            admin=True,
            priority=6,
        ),
        Route(
            re.compile(_RE_TOGGLE, re.IGNORECASE),
            "mh_toggle",
            "功能开关（管理员）",
            run_toggle,
            admin=True,
            priority=6,
        ),
    ]
