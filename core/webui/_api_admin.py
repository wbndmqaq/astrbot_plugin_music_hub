"""WebUI REST API handlers（总览 / 统计 / 配置 / 黑白名单）。"""

from __future__ import annotations

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
        keep = bool(body.get("keepTotals", True))
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
                    cfg.set(key, expect(value) if expect is not str else str(value))
                    applied.append(key)
                except (TypeError, ValueError):
                    rejected.append(key)
            elif key in ("ncm", "kg", "qq") and isinstance(value, dict):
                src = key
                node = dict(cfg.src_node(src))
                for skey, sval in value.items():
                    if skey in _EDITABLE_SRC_KEYS:
                        expect = _EDITABLE_SRC_KEYS[skey]
                        if skey == "quality" and sval not in _SRC_QUALITY[src]:
                            rejected.append(f"{src}.{skey}")
                            continue
                        try:
                            node[skey] = expect(sval) if expect is not str else str(sval).strip()
                            applied.append(f"{src}.{skey}")
                        except (TypeError, ValueError):
                            rejected.append(f"{src}.{skey}")
                if src == "ncm" or src == "kg":
                    node["apiBase"] = normalize_base(str(node.get("apiBase", "")))
                cfg.set(src, node)
            elif key == "scheduler" and isinstance(value, dict):
                node = dict(cfg.sched_node())
                if "enable" in value:
                    node["enable"] = bool(value["enable"])
                if "signinHour" in value:
                    try:
                        node["signinHour"] = max(0, min(23, int(value["signinHour"])))
                    except (TypeError, ValueError):
                        rejected.append("scheduler.signinHour")
                if "ncmSignin" in value:
                    node["ncmSignin"] = bool(value["ncmSignin"])
                if "qqRefresh" in value:
                    node["qqRefresh"] = bool(value["qqRefresh"])
                cfg.set("scheduler", node)
                applied.append("scheduler")
            elif key == "webui" and isinstance(value, dict):
                node = dict(cfg.webui_node())
                if "enable" in value:
                    node["enable"] = bool(value["enable"])
                if "port" in value:
                    try:
                        node["port"] = max(1, min(65535, int(value["port"])))
                    except (TypeError, ValueError):
                        rejected.append("webui.port")
                if "host" in value:
                    node["host"] = str(value["host"]).strip() or "0.0.0.0"
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
                "note": "部分改动需重载插件生效" if "webui" in " ".join(applied) else "",
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

        server.config.set_acl(
            mode=mode,
            blacklist=_clean(body.get("blacklist")) if "blacklist" in body else None,
            whitelist=_clean(body.get("whitelist")) if "whitelist" in body else None,
        )
        saved = await server.config.save_async()
        return _json({"ok": True, "saved": saved})

    return handler
