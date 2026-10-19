"""WebUI REST API handlers（总览 / 统计 / 配置 / 黑白名单）。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from aiohttp import web
from astrbot.api import logger

from .. import SOURCE_NAMES, SOURCES
from ..config import normalize_base
from ..media import _is_internal_host
from ._auth import client_ip
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
                    # 配置开着但客户端自检不过（依赖缺失/初始化失败）：总览要能
                    # 显示「依赖缺失」，否则用户看着「匿名可用」却搜不到任何结果
                    "ready": service.is_ready(src) if enabled else True,
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
        # 审计留痕（对齐改密/ACL/配置变更口径）：清统计是「抹掉失败原因证据」的
        # 动作，被窃会话可以在排查前清场
        peer = getattr(request, "remote", "") or ""
        headers = getattr(request, "headers", None) or {}
        origin = (
            client_ip(peer, headers.get("X-Forwarded-For", ""), server.config.webui_trusted_proxy)
            or "未知来源"
        )
        logger.info(f"[music_hub] WebUI 统计重置（keepTotals={keep}，{origin}）")
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
    "apiTimeoutSec": int,
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
    "apiTimeoutSec": (5, 120),
}
# 嵌套节点里的 int 键，键名为 "<节点>.<字段>"
_NESTED_INT_RANGES: dict[str, tuple[int, int]] = {
    "webui.port": (1, 65535),
    "webui.sessionHours": (1, 168),
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
    str 同样严格：str(["a"]) 会把 list 硬转成 "['a']" 这类 Junk 写进配置，
    与 apiBase 分支的 isinstance 守卫同一哲学（类型混淆全族拒绝）。
    """
    if expect is bool:
        if isinstance(value, bool):
            return value
        raise TypeError(f"{key} 需要布尔值，收到 {type(value).__name__}")
    if expect is int:
        return clamp_int(key, value)
    if not isinstance(value, str):
        raise TypeError(f"{key} 需要字符串，收到 {type(value).__name__}")
    text = value.strip()
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

# 音质档位白名单必须与 core.quality 各阶梯一致（kg 只有旧 /song/url 支持的三档，
# viper/super/high 属新端点、对现用阶梯无效 —— 见 quality.py 的排除说明；
# ncm 需含 vivid，与 _conf_schema.json 的 options 对齐）。
_SRC_QUALITY = {
    "ncm": {
        "auto",
        "standard",
        "higher",
        "exhigh",
        "lossless",
        "hires",
        "jyeffect",
        "vivid",
        "sky",
        "dolby",
        "jymaster",
    },
    "kg": {"auto", "128", "320", "flac"},
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
        out["apiTimeoutSec"] = cfg.api_timeout_sec
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
        out["webui"] = {
            "enable": cfg.webui_enable,
            "host": cfg.webui_host,
            "port": cfg.webui_port,
            "sessionHours": cfg.webui_session_hours,
            "hostAllowlist": cfg.webui_host_allowlist,
            "secureCookie": cfg.webui_secure_cookie,
        }
        return _json({"config": out})

    return handler


def make_config_save(server):
    async def handler(request: web.Request) -> web.Response:
        cfg = server.config
        body = await _body(request)
        patch = body.get("config") if isinstance(body.get("config"), dict) else body
        applied, rejected = [], []
        reasons: dict[str, str] = {}
        bind_warn = ""  # 非阻断性提示（经响应 note 下发，不回滚用户改动）

        def reject(key: str, reason: str = "") -> None:
            # rejected 只记键名：前端按键回填服务端真实值（按点号拆键定位控件），
            # 混入长说明串会破坏回填；原因放独立的 rejectedReasons 供排查展示
            rejected.append(key)
            if reason:
                reasons[key] = reason

        for key, value in (patch or {}).items():
            if key in _EDITABLE_KEYS:
                expect = _EDITABLE_KEYS[key]
                try:
                    coerced = coerce_setting(key, expect, value)
                except (TypeError, ValueError) as e:
                    reject(key, str(e))
                    continue
                # set 失败（底层配置对象行为异常）不能计入 applied 谎报生效
                if cfg.set(key, coerced):
                    applied.append(key)
                else:
                    reject(key, "写入底层配置失败，详情见服务端日志")
            elif key in ("ncm", "kg", "qq") and isinstance(value, dict):
                src = key
                node = dict(cfg.src_node(src))
                node_applied: list[str] = []
                for skey, sval in value.items():
                    # QQ 走内置库直连，apiBase 无任何消费者（src_api_base("qq") 恒返回
                    # 哨兵 "内置"，config_get 也不回显）：写侧放行会造出「面板收下了却永不
                    # 生效」的假配置，按未知键拒绝而非读侧回显
                    if skey not in _EDITABLE_SRC_KEYS or (src == "qq" and skey == "apiBase"):
                        reject(f"{src}.{skey}")
                        continue
                    expect = _EDITABLE_SRC_KEYS[skey]
                    # 先校验类型：sval 来自 JSON body，传 list 时
                    # `sval not in set` 会因 list 不可哈希抛 TypeError，
                    # 穿透到 _handle_errors 变成 500 而非 400
                    if skey == "quality":
                        if not isinstance(sval, str) or sval not in _SRC_QUALITY[src]:
                            reject(f"{src}.{skey}", f"音质必须是 {'/'.join(_SRC_QUALITY[src])} 之一")
                            continue
                    if skey == "apiBase" and (src == "ncm" or src == "kg"):
                        # 非 JSON 字符串（dict/list/数字）先拒：str() 强转会把
                        # "{'x': 1}" 之类 Junk 补上 http:// 后混过 scheme 检查写进配置，
                        # 请求时 aiohttp 抛 InvalidURL 误导排障（与 quality 分支同口径）
                        if not isinstance(sval, str):
                            reject(f"{src}.{skey}", "API 地址必须是字符串")
                            continue
                        # 与聊天端 run_api 同口径：normalize 后只放行 http/https，
                        # file:// 等协议不能经面板写进配置再由服务器发起请求。
                        # 同时拒绝回环/私网/链路本地地址：持有会话者可借
                        # /api/service/check 的状态码差异对内网做存活探测
                        normalized = normalize_base(sval)
                        if normalized and not normalized.lower().startswith(("http://", "https://")):
                            reject(f"{src}.{skey}", "API 地址必须以 http:// 或 https:// 开头")
                            continue
                        host = urlparse(normalized).hostname or ""
                        if _is_internal_host(host):
                            reject(
                                f"{src}.{skey}",
                                "拒绝内网/回环地址：面板会向该地址发起请求，"
                                "填公网可访问的 API 服务地址（本机自建请用公网地址或反向代理）",
                            )
                            continue
                        # 不做 DNS 解析级校验（解析到私网的公网域名可绕过字面校验，
                        # 已声明接受）：fake-ip 代理（Clash TUN 等）会把所有域名解析到
                        # 198.18.0.0/15 保留网段、内网自建主机名只有内网 DNS 能解，
                        # 写入时解析会误杀这两类合法部署；请求侧连接失败自会暴露
                        node[skey] = normalized
                        node_applied.append(f"{src}.{skey}")
                        continue
                    try:
                        node[skey] = coerce_setting(f"{src}.{skey}", expect, sval)
                        node_applied.append(f"{src}.{skey}")
                    except (TypeError, ValueError) as e:
                        reject(f"{src}.{skey}", str(e))
                # 仅至少一个字段落盘才 set：全拒 / 全未知时原样回写等于无意义写盘，
                # 且会让「空保存」也走 save_async、记账与实际写入脱节。节点级 set
                # 失败时整批字段转入 rejected，绝不留在 applied 里
                if node_applied:
                    if cfg.set(src, node):
                        applied.extend(node_applied)
                    else:
                        for applied_key in node_applied:
                            reject(applied_key, "写入底层配置失败，详情见服务端日志")
            elif key == "scheduler" and isinstance(value, dict):
                node = dict(cfg.sched_node())
                changed = False
                for skey in ("enable", "ncmSignin", "qqRefresh"):
                    if skey in value:
                        try:
                            node[skey] = coerce_setting(f"scheduler.{skey}", bool, value[skey])
                            changed = True
                        except (TypeError, ValueError) as e:
                            reject(f"scheduler.{skey}", str(e))
                if "signinHour" in value:
                    try:
                        node["signinHour"] = clamp_int("scheduler.signinHour", value["signinHour"])
                        changed = True
                    except (TypeError, ValueError) as e:
                        reject("scheduler.signinHour", str(e))
                # 与音源节点分支同一记账纪律：一个字段都没改成时不算 applied、不写盘
                if changed:
                    if cfg.set("scheduler", node):
                        applied.append("scheduler")
                    else:
                        reject("scheduler", "写入底层配置失败，详情见服务端日志")
            elif key == "webui" and isinstance(value, dict):
                node = dict(cfg.webui_node())
                changed = False
                if "enable" in value:
                    try:
                        node["enable"] = coerce_setting("webui.enable", bool, value["enable"])
                        changed = True
                    except (TypeError, ValueError) as e:
                        reject("webui.enable", str(e))
                if "port" in value:
                    try:
                        node["port"] = clamp_int("webui.port", value["port"])
                        changed = True
                    except (TypeError, ValueError) as e:
                        reject("webui.port", str(e))
                if "sessionHours" in value:
                    try:
                        node["sessionHours"] = clamp_int("webui.sessionHours", value["sessionHours"])
                        changed = True
                    except (TypeError, ValueError) as e:
                        reject("webui.sessionHours", str(e))
                if "host" in value:
                    # 必须校验：host 配错会让 start() 绑定失败，面板起不来，
                    # 而修复它的唯一入口正是这个面板本身 —— 只能手改配置文件。
                    # 空值必须映射 127.0.0.1（与读取侧 config.webui_host 的回落一致）：
                    # 兜底成 0.0.0.0 会让「清空监听地址」静默绑到全部网卡 —— 面板能改
                    # 全部配置、导出平台 Cookie，扩大暴露面违背清空者的本意
                    raw_val = value["host"]
                    if not isinstance(raw_val, str):
                        # str(123) → "123" 恰好能过 _valid_bind_host 的主机名正则，
                        # 重启绑 "123" 必失败且无自救入口——类型混淆按拒绝处理
                        reject("webui.host", f"host 需要字符串，收到 {type(raw_val).__name__}")
                    else:
                        raw = raw_val.strip() or "127.0.0.1"
                        if _valid_bind_host(raw):
                            node["host"] = raw
                            changed = True
                            if raw in ("0.0.0.0", "::") and not node.get("secureCookie"):
                                # 管理员自选暴露面不拦（rejected 会把改动回滚成没保存），
                                # 但改完到重载前的启动告警不再触发：在响应 note 里当面提示
                                bind_warn = (
                                    "0.0.0.0/:: 会把面板暴露到所有网卡，"
                                    "建议同步开启 secureCookie 并经 https 反代访问，或用防火墙限制来源；"
                                    "裸暴露还面临慢速连接占位（aiohttp 对半截请求头无读取超时），"
                                    "请务必前置带读取超时的反向代理"
                                )
                        else:
                            reject("webui.host")
                if "hostAllowlist" in value:
                    # Host 白名单：只收字符串列表，**整键全有或全无**——任一登记项
                    # 非法就整个键拒绝（部分应用会造出「合法项已生效、坏项只躺在
                    # rejected 里」的半成品状态，排查困难）。逐项去空白/去前导点/
                    # 小写（与读取侧 webui_host_allowlist 的归一一致）；带端口/根
                    # 尾点的登记项会让 _host_ok 永远 403（比对的是剥端口后的裸
                    # 主机名）：写入侧直接打回并给出可操作的报错，而不是让用户
                    # 配完白名单反而登不上面板
                    raw = value["hostAllowlist"]
                    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
                        cleaned, bad_items = [], []
                        for d in raw:
                            item = d.strip().lower().lstrip(".")
                            if not item:
                                continue
                            if ":" in item or item.endswith("."):
                                bad_items.append(item)
                                continue
                            # FQDN 上限 253 字符；超长「域名」和上不封顶的条数
                            # 原样进配置后，读侧 _host_ok 每请求线性扫描会被拖慢
                            if len(item) > 253:
                                bad_items.append(item[:32] + "…")
                                continue
                            cleaned.append(item)
                        if len(cleaned) > 32:
                            reject("webui.hostAllowlist", "白名单条目过多（最多 32 条）")
                        elif bad_items:
                            reject(
                                "webui.hostAllowlist",
                                "只收裸域名（≤253 字符），不含端口与根尾点：" + "、".join(bad_items[:3]),
                            )
                        else:
                            # 空列表也是合法值（清空白名单）
                            node["hostAllowlist"] = cleaned
                            changed = True
                    else:
                        reject("webui.hostAllowlist", "只收字符串数组")
                if "secureCookie" in value:
                    try:
                        node["secureCookie"] = coerce_setting(
                            "webui.secureCookie", bool, value["secureCookie"]
                        )
                        changed = True
                    except (TypeError, ValueError) as e:
                        reject("webui.secureCookie", str(e))
                # 未知子键显式 reject（与顶层键 / 音源未知子键同纪律）：trustedProxy、
                # password 等面板不可写键被静默吞掉会让管理员的输入无下落
                for skey in value:
                    if skey not in (
                        "enable",
                        "port",
                        "sessionHours",
                        "host",
                        "hostAllowlist",
                        "secureCookie",
                    ):
                        reject(f"webui.{skey}")
                if changed:
                    if cfg.set("webui", node):
                        applied.append("webui")
                    else:
                        reject("webui", "写入底层配置失败，详情见服务端日志")
            else:
                reject(key)
        # 零改动（全部被拒）不落盘：无谓写盘且与 AstrBot 核心配置写入存在丢更新窗口
        if applied:
            # 审计：只记键名不记值——secret 键的值落进日志等于泄漏；来源 IP 与
            # 登录限速同一口径（client_ip）
            peer = getattr(request, "remote", "") or ""
            headers = getattr(request, "headers", None) or {}
            origin = (
                client_ip(peer, headers.get("X-Forwarded-For", ""), cfg.webui_trusted_proxy) or "未知来源"
            )
            logger.info(f"[music_hub] WebUI 配置变更（{origin}）：{'、'.join(applied)}")
        saved = await cfg.save_async() if applied else False
        if "rateLimitMs" in applied:
            from ..ratelimit import limiter

            limiter.update_interval(cfg.rate_limit_ms)
        return _json(
            {
                "ok": len(applied) > 0,
                "applied": applied,
                "rejected": rejected,
                "rejectedReasons": reasons,
                "saved": saved,
                "note": "；".join(
                    n
                    for n in (
                        "部分改动需重载插件生效" if any(k.startswith("webui") for k in applied) else "",
                        bind_warn,
                    )
                    if n
                ),
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


# 名单规模上限：单条 64 字符足够容纳任何真实用户/群 ID——1MB body 里塞一条
# 超长字符串会原样写进配置；总条数防名单被撑爆后拖慢每一次 ACL 检查
_ACL_ITEM_MAX = 64
_ACL_LIST_MAX = 500


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
        # 规模上限同样在写侧拒绝：超限条目混进配置等于静默放行攻击面
        for kind in ("blacklist", "whitelist"):
            if kind in body and body[kind] is not None and not isinstance(body[kind], list):
                return _json({"error": f"{kind} 必须是字符串数组"}, 400)
            items = body.get(kind)
            if isinstance(items, list):
                if len(items) > _ACL_LIST_MAX:
                    return _json({"error": f"{kind} 条目过多（上限 {_ACL_LIST_MAX} 条）"}, 400)
                if any(len(str(it).strip()) > _ACL_ITEM_MAX for it in items):
                    return _json({"error": f"{kind} 单条过长（上限 {_ACL_ITEM_MAX} 字符）"}, 400)

        # set 失败（底层配置对象异常）必须如实上报：黑白名单没写进去却回 ok:true，
        # 管理员会误以为拉黑已生效
        applied = server.config.set_acl(
            mode=mode,
            blacklist=_clean(body["blacklist"]) if isinstance(body.get("blacklist"), list) else None,
            whitelist=_clean(body["whitelist"]) if isinstance(body.get("whitelist"), list) else None,
        )
        if not applied:
            logger.warning("[music_hub] WebUI ACL 写入底层配置失败（黑白名单未生效）")
            return _json({"error": "写入底层配置失败，黑白名单未生效，详情见服务端日志"}, 500)
        # 审计：记动作与规模不记条目内容（名单值进日志等于把拉黑对象写成日志）
        logger.info(
            "[music_hub] WebUI ACL 变更：mode=%s black=%d white=%d",
            mode if mode is not None else "(不变)",
            len(body["blacklist"]) if isinstance(body.get("blacklist"), list) else -1,
            len(body["whitelist"]) if isinstance(body.get("whitelist"), list) else -1,
        )
        saved = await server.config.save_async()
        return _json({"ok": True, "saved": saved})

    return handler
