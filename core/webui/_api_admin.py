"""WebUI REST API handlers（总览 / 统计 / 配置 / 黑白名单）。"""

from __future__ import annotations

import ipaddress
import re

from aiohttp import web

from .. import SOURCE_NAMES, SOURCES
from ..config import normalize_base
from ._util import body_json as _body
from ._util import json_response as _json


# ──────────── 总览 / 统计 ────────────
def make_overview(server):
    async def handler(request: web.Request) -> web.Response:
        service = server.service
        cfg = server.config
        sources = []
        for src in SOURCES:
            api_base = cfg.src_api_base(src)
            enabled = cfg.src_enabled(src)
            logged = bool(cfg.src_cookie(src))
            sources.append(
                {
                    "source": src,
                    "name": SOURCE_NAMES[src],
                    "enabled": enabled,
                    "logged": logged,
                    "quality": cfg.src_quality(src),
                    "apiBase": api_base if src == "qq" else ("已配置" if enabled else "未配置"),
                }
            )
        stats = service.stats.snapshot(days=1)
        return _json(
            {
                "version": server._version(),
                "enable": cfg.enable,
                "webui": {"port": cfg.webui_port},
                "sources": sources,
                "todayTotal": stats.get("todayTotal", 0),
                "totalAll": stats.get("totalAll", 0),
                "today": stats.get("today", {}),
                "total": stats.get("total", {}),
                "recent": stats.get("recent", [])[:8],
                "acl": {
                    "mode": cfg.acl_mode,
                    "black": len(cfg.acl_list("blacklist")),
                    "white": len(cfg.acl_list("whitelist")),
                },
            }
        )

    return handler


def make_stats(server):
    async def handler(request: web.Request) -> web.Response:
        days = 14
        try:
            days = max(3, min(30, int(request.query.get("days", "14"))))
        except ValueError:
            pass
        return _json(server.service.stats.snapshot(days))

    return handler


def make_stats_reset(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        # 严格 bool：bool("false") 是 True，宽松强转会把「保留总计」的请求
        # 反向变成「清空总计」，与 coerce_setting 防的是同一个陷阱
        keep = body.get("keepTotals", True)
        if not isinstance(keep, bool):
            return _json({"error": "keepTotals 必须是布尔值"}, 400)
        server.service.stats.reset(keep_totals=keep)
        await server.service.stats.flush()
        return _json({"ok": True})

    return handler


# ──────────── 配置 ────────────
# 允许通过 WebUI 修改的配置键（白名单，防止改坏 AstrBot 核心配置）
_EDITABLE_KEYS = {
    "enable": bool,
    "defaultSource": str,
    "maxList": int,
    "enableSongRequest": bool,
    "enableResolve": bool,
    "resolveCards": bool,
    "renderListCard": bool,
    "sendTextInfo": bool,
    "identifyPrefix": str,
    "sendVocal": bool,
    "uploadFile": bool,
    "disableHighQualityVocal": bool,
    "ffmpegCompress": bool,
    "compressBitrate": int,
    "downloadTimeout": int,
    "keepFileSec": int,
    "sendNativeCard": bool,
    "qqofficialAdapt": bool,
    "qqofficialChunkedUpload": bool,
    "cooldownSec": int,
    "rateLimitMs": int,
}
_EDITABLE_SRC_KEYS = {"apiBase": str, "quality": str, "qualityUnblock": bool, "trialFallback": bool}

# 可写 int 键的合法区间。缺了这里的声明就能被写入越界值：
# cooldownSec=-1 会让 check_cooldown 恒返回 None（点歌冷却被完全禁用），
# maxList=-1 会让下游切片行为异常。与 config.py 的 lo/hi 兜底保持一致。
_INT_RANGES: dict[str, tuple[int, int]] = {
    "maxList": (1, 20),
    "compressBitrate": (32, 320),
    "downloadTimeout": (5000, 600000),
    "keepFileSec": (5, 3600),
    "cooldownSec": (0, 600),
    "rateLimitMs": (0, 5000),
}
# 嵌套节点里的 int 键，键名为 "<节点>.<字段>"
_NESTED_INT_RANGES: dict[str, tuple[int, int]] = {
    "webui.port": (1, 65535),
    "scheduler.signinHour": (0, 23),
}


def clamp_int(key: str, value):
    """按声明的区间夹取 int 配置值；无区间声明时返回转换结果。

    非法值统一抛 TypeError/ValueError 由调用方归入 rejected。捕获面必须含
    OverflowError：JSON 的 1e999 解析成 float('inf')，int() 对它抛 OverflowError，
    漏掉会穿透成 500 而非 rejected。bool 显式拒绝（bool 是 int 子类，
    int(True)==1 会把 true/false 混进数值配置），与 coerce_setting 同一严格哲学。
    """
    if isinstance(value, bool):
        raise TypeError(f"{key} 需要整数，收到布尔值")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise ValueError(f"{key} 不是合法整数") from e
    bounds = _INT_RANGES.get(key) or _NESTED_INT_RANGES.get(key)
    if bounds:
        lo, hi = bounds
        result = max(lo, min(hi, result))
    return result


def coerce_setting(key: str, expect: type, value):
    """按 schema 声明类型强转配置值；非法值抛 TypeError/ValueError 由调用方归入 rejected。

    bool 必须严格校验：bool("false") / bool("0") 都是 True，宽松强转会让
    「关闭某功能」的请求反向把功能打开 —— 对管理面板是严重缺陷。
    """
    if expect is bool:
        if isinstance(value, bool):
            return value
        raise TypeError(f"{key} 需要布尔值，收到 {type(value).__name__}")
    if expect is int:
        return clamp_int(key, value)
    text = str(value).strip()
    if key == "defaultSource" and text not in _DEFAULT_SOURCES:
        # 读取侧对白名单外的值静默回落 auto，写侧放行会让面板显示值与实际生效值漂移
        raise ValueError(f"{key} 仅支持 auto/ncm/kg/qq")
    if key == "identifyPrefix":
        # 识别前缀原样拼进发送文本：换行/控制字符能伪造消息结构，超长会刷屏
        if len(text) > 20 or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise ValueError(f"{key} 最长 20 字符且不含换行/控制字符")
    return text


def _valid_bind_host(raw: str) -> bool:
    """监听地址是否可绑定：IP 字面量或合法主机名。

    非法值会让重载后 TCPSite 绑定失败 → 面板起不来，且无自救入口，必须拒写。
    """
    try:
        ipaddress.ip_address(raw)
        return True
    except ValueError:
        return bool(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", raw))


# defaultSource 值域白名单：集合必须与读取侧 config.default_source 的合法集合一致
_DEFAULT_SOURCES = {"auto", *SOURCES}

_SRC_QUALITY = {
    "ncm": {
        "auto",
        "standard",
        "higher",
        "exhigh",
        "lossless",
        "hires",
        "jyeffect",
        "sky",
        "dolby",
        "jymaster",
    },
    "kg": {"auto", "128", "320", "flac", "high", "super", "viper_clear", "viper_tape"},
    "qq": {"auto", "128", "320", "flac", "atmos", "master", "atmos_db"},
}


def make_config_get(server):
    async def handler(request: web.Request) -> web.Response:
        cfg = server.config
        typed = {
            "maxList": cfg.max_list,
            "identifyPrefix": cfg.identify_prefix,
            "compressBitrate": cfg.compress_bitrate,
            "downloadTimeout": int(cfg.download_timeout * 1000),
            "keepFileSec": cfg.keep_file_sec,
        }
        out = {}
        for key in _EDITABLE_KEYS:
            out[key] = typed[key] if key in typed else cfg.get(key)
        bools = {
            "enable": cfg.enable,
            "enableSongRequest": cfg.enable_song_request,
            "enableResolve": cfg.enable_resolve,
            "resolveCards": cfg.resolve_cards,
            "renderListCard": cfg.render_list_card,
            "sendTextInfo": cfg.send_text_info,
            "sendVocal": cfg.send_vocal,
            "uploadFile": cfg.upload_file,
            "disableHighQualityVocal": cfg.disable_high_quality_vocal,
            "ffmpegCompress": cfg.ffmpeg_compress,
            "sendNativeCard": cfg.send_native_card,
            "qqofficialAdapt": cfg.qq_official_adapt,
            "qqofficialChunkedUpload": cfg.qq_official_chunked,
        }
        for k, v in bools.items():
            if cfg.get(k) is None:
                out[k] = v
        out["defaultSource"] = cfg.default_source
        out["cooldownSec"] = cfg.cooldown_sec
        out["rateLimitMs"] = int(cfg.rate_limit_ms)
        out["scheduler"] = {
            "enable": cfg.scheduler_enable,
            "signinHour": cfg.scheduler_signin_hour,
            "ncmSignin": cfg.scheduler_ncm_signin,
            "qqRefresh": cfg.scheduler_qq_refresh,
        }
        out["ncm"] = {
            "apiBase": cfg.src_api_base("ncm"),
            "quality": cfg.src_quality("ncm"),
            "qualityUnblock": cfg.src_quality_unblock("ncm"),
            "hasCookie": bool(cfg.src_cookie("ncm")),
        }
        out["kg"] = {
            "apiBase": cfg.src_api_base("kg"),
            "quality": cfg.src_quality("kg"),
            "trialFallback": cfg.src_quality_unblock("kg"),
            "hasCookie": bool(cfg.src_cookie("kg")),
        }
        out["qq"] = {
            "quality": cfg.src_quality("qq"),
            "trialFallback": cfg.src_quality_unblock("qq"),
            "hasCredential": bool(cfg.src_cookie("qq")),
        }
        out["webui"] = {"enable": cfg.webui_enable, "host": cfg.webui_host, "port": cfg.webui_port}
        return _json({"config": out})

    return handler


def make_config_save(server):
    async def handler(request: web.Request) -> web.Response:
        cfg = server.config
        body = await _body(request)
        patch = body.get("config") if isinstance(body.get("config"), dict) else body
        applied, rejected = [], []
        for key, value in (patch or {}).items():
            if key in _EDITABLE_KEYS:
                expect = _EDITABLE_KEYS[key]
                try:
                    cfg.set(key, coerce_setting(key, expect, value))
                    applied.append(key)
                except (TypeError, ValueError):
                    rejected.append(key)
            elif key in ("ncm", "kg", "qq") and isinstance(value, dict):
                src = key
                node = dict(cfg.src_node(src))
                changed = False
                for skey, sval in value.items():
                    # QQ 走内置库直连，apiBase 无任何消费者（src_api_base("qq") 恒返回
                    # "local"，config_get 也不回显）：写侧放行会造出「面板收下了却永不
                    # 生效」的假配置，按未知键拒绝而非读侧回显
                    if skey not in _EDITABLE_SRC_KEYS or (src == "qq" and skey == "apiBase"):
                        rejected.append(f"{src}.{skey}")
                        continue
                    expect = _EDITABLE_SRC_KEYS[skey]
                    # 先校验类型：sval 来自 JSON body，传 list 时
                    # `sval not in set` 会因 list 不可哈希抛 TypeError，
                    # 穿透到 _handle_errors 变成 500 而非 400
                    if skey == "quality":
                        if not isinstance(sval, str) or sval not in _SRC_QUALITY[src]:
                            rejected.append(f"{src}.{skey}")
                            continue
                    try:
                        node[skey] = coerce_setting(f"{src}.{skey}", expect, sval)
                        applied.append(f"{src}.{skey}")
                        changed = True
                    except (TypeError, ValueError):
                        rejected.append(f"{src}.{skey}")
                # 仅至少一个字段落盘才 set：全拒 / 全未知时原样回写等于无意义写盘，
                # 且会让「空保存」也走 save_async、记账与实际写入脱节
                if changed:
                    if src == "ncm" or src == "kg":
                        node["apiBase"] = normalize_base(str(node.get("apiBase", "")))
                    cfg.set(src, node)
            elif key == "scheduler" and isinstance(value, dict):
                node = dict(cfg.sched_node())
                changed = False
                for skey in ("enable", "ncmSignin", "qqRefresh"):
                    if skey in value:
                        try:
                            node[skey] = coerce_setting(f"scheduler.{skey}", bool, value[skey])
                            changed = True
                        except (TypeError, ValueError):
                            rejected.append(f"scheduler.{skey}")
                if "signinHour" in value:
                    try:
                        node["signinHour"] = clamp_int("scheduler.signinHour", value["signinHour"])
                        changed = True
                    except (TypeError, ValueError):
                        rejected.append("scheduler.signinHour")
                # 与音源节点分支同一记账纪律：一个字段都没改成时不算 applied、不写盘
                if changed:
                    cfg.set("scheduler", node)
                    applied.append("scheduler")
            elif key == "webui" and isinstance(value, dict):
                node = dict(cfg.webui_node())
                changed = False
                if "enable" in value:
                    try:
                        node["enable"] = coerce_setting("webui.enable", bool, value["enable"])
                        changed = True
                    except (TypeError, ValueError):
                        rejected.append("webui.enable")
                if "port" in value:
                    try:
                        node["port"] = clamp_int("webui.port", value["port"])
                        changed = True
                    except (TypeError, ValueError):
                        rejected.append("webui.port")
                if "host" in value:
                    # 必须校验：host 配错会让 start() 绑定失败，面板起不来，
                    # 而修复它的唯一入口正是这个面板本身 —— 只能手改配置文件。
                    # 空值必须映射 127.0.0.1（与读取侧 config.webui_host 的回落一致）：
                    # 兜底成 0.0.0.0 会让「清空监听地址」静默绑到全部网卡 —— 面板能改
                    # 全部配置、导出平台 Cookie，扩大暴露面违背清空者的本意
                    raw = str(value["host"]).strip() or "127.0.0.1"
                    if _valid_bind_host(raw):
                        node["host"] = raw
                        changed = True
                    else:
                        rejected.append("webui.host")
                if changed:
                    cfg.set("webui", node)
                    applied.append("webui")
            else:
                rejected.append(key)
        saved = await cfg.save_async()
        if "rateLimitMs" in applied:
            from ..ratelimit import limiter

            limiter.update_interval(cfg.rate_limit_ms)
        return _json(
            {
                "ok": len(applied) > 0,
                "applied": applied,
                "rejected": rejected,
                "saved": saved,
                "note": "部分改动需重载插件生效" if any(k.startswith("webui") for k in applied) else "",
            }
        )

    return handler


# ──────────── 黑白名单 ────────────
def make_acl_get(server):
    async def handler(request: web.Request) -> web.Response:
        cfg = server.config
        return _json(
            {
                "mode": cfg.acl_mode,
                "blacklist": cfg.acl_list("blacklist"),
                "whitelist": cfg.acl_list("whitelist"),
            }
        )

    return handler


def make_acl_save(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        mode = body.get("mode")
        if mode is not None and mode not in ("off", "blacklist", "whitelist"):
            return _json({"error": "mode 必须是 off/blacklist/whitelist"}, 400)

        def _clean(items) -> list[str]:
            if not isinstance(items, list):
                return []
            out, seen = [], set()
            for it in items:
                s = str(it).strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
            return out

        # 严格 list：_clean 对非 list 返回 []，宽松路径会把 {"blacklist": "123456"}
        # 变成「名单抹零」且响应仍 ok —— 宽松强转会反向变形请求（与 keepTotals /
        # coerce_setting 同一哲学）。None 是「不改该项」的语义，放行交回 set_acl。
        for kind in ("blacklist", "whitelist"):
            if kind in body and body[kind] is not None and not isinstance(body[kind], list):
                return _json({"error": f"{kind} 必须是字符串数组"}, 400)

        server.config.set_acl(
            mode=mode,
            blacklist=_clean(body["blacklist"]) if isinstance(body.get("blacklist"), list) else None,
            whitelist=_clean(body["whitelist"]) if isinstance(body.get("whitelist"), list) else None,
        )
        saved = await server.config.save_async()
        return _json({"ok": True, "saved": saved})

    return handler
