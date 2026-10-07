"""配置访问助手。

包装 AstrBotConfig（dict 子类），提供类型安全的读取与保存。所有取值都有
默认值兜底，脏配置（WebUI 改坏、旧版本残留）不会让插件崩掉。
"""

from __future__ import annotations

from typing import Any

from . import DEFAULT_API_BASE, SOURCES


def as_int(value: Any, default: int = 0, lo: int | None = None, hi: int | None = None) -> int:
    """宽松转 int：bool/float/str/None 都不炸，NaN 安全。"""
    try:
        if value is None or value is True or value is False:
            return default
        result = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    if lo is not None and result < lo:
        return lo
    if hi is not None and result > hi:
        return hi
    return result


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "y", "开")
    return default


def as_str(value: Any, default: str = "") -> str:
    if value is None or value is True or value is False:
        return default
    if isinstance(value, str):
        return value
    return default


def as_list(value: Any) -> list:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    if isinstance(value, tuple):
        return [v for v in value if isinstance(v, str)]
    return []


def normalize_base(url: str) -> str:
    """规范化 API base：去尾部斜杠，补 http:// 前缀（用户常忘写协议）。"""
    url = as_str(url).strip().rstrip("/")
    if not url:
        return ""
    if url == "http://" or url == "https://":
        return ""
    if "://" not in url:
        url = "http://" + url
    return url


class Config:
    """插件配置的强类型门面。"""

    def __init__(self, raw: Any):
        self._raw = raw  # AstrBotConfig（dict 子类）；测试环境传 dict

    # ──────────── 基础 ────────────
    def get(self, key: str, default: Any = None) -> Any:
        try:
            value = self._raw.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def set(self, key: str, value: Any) -> None:
        try:
            self._raw[key] = value
        except Exception:
            pass

    def save(self) -> bool:
        """保存配置（同步）。**事件循环上请用 :meth:`save_async`**，此方法供非异步上下文。"""
        save = getattr(self._raw, "save_config", None)
        if save is None:
            return False
        try:
            result = save()
        except Exception:
            return False
        return result if isinstance(result, bool) else True

    async def save_async(self) -> bool:
        """事件循环安全的保存：磁盘写在 to_thread（AstrBotConfig.save_config 是同步落盘）。"""
        import asyncio

        return await asyncio.to_thread(self.save)

    # ──────────── 全局 ────────────
    @property
    def enable(self) -> bool:
        return as_bool(self.get("enable"), True)

    @property
    def default_source(self) -> str:
        src = as_str(self.get("defaultSource"), "auto")
        return src if src in ("auto", *SOURCES) else "auto"

    @property
    def max_list(self) -> int:
        return as_int(self.get("maxList"), 10, lo=1, hi=20)

    @property
    def enable_song_request(self) -> bool:
        return as_bool(self.get("enableSongRequest"), True)

    @property
    def enable_resolve(self) -> bool:
        return as_bool(self.get("enableResolve"), True)

    @property
    def resolve_cards(self) -> bool:
        return as_bool(self.get("resolveCards"), True)

    @property
    def render_list_card(self) -> bool:
        return as_bool(self.get("renderListCard"), True)

    @property
    def send_text_info(self) -> bool:
        return as_bool(self.get("sendTextInfo"), True)

    @property
    def identify_prefix(self) -> str:
        return as_str(self.get("identifyPrefix"), "识别：")

    # ──────────── 发送通道 ────────────
    @property
    def send_vocal(self) -> bool:
        return as_bool(self.get("sendVocal"), True)

    @property
    def upload_file(self) -> bool:
        return as_bool(self.get("uploadFile"), True)

    @property
    def disable_high_quality_vocal(self) -> bool:
        return as_bool(self.get("disableHighQualityVocal"), False)

    @property
    def ffmpeg_compress(self) -> bool:
        return as_bool(self.get("ffmpegCompress"), True)

    @property
    def compress_bitrate(self) -> int:
        return as_int(self.get("compressBitrate"), 128, lo=32, hi=320)

    @property
    def download_timeout(self) -> float:
        return as_int(self.get("downloadTimeout"), 120000, lo=5000, hi=600000) / 1000.0

    @property
    def keep_file_sec(self) -> int:
        return max(5, as_int(self.get("keepFileSec"), 60, lo=0))

    @property
    def send_native_card(self) -> bool:
        return as_bool(self.get("sendNativeCard"), False)

    @property
    def qq_official_adapt(self) -> bool:
        return as_bool(self.get("qqofficialAdapt"), True)

    @property
    def qq_official_chunked(self) -> bool:
        return as_bool(self.get("qqofficialChunkedUpload"), True)

    # ──────────── 音源子配置 ────────────
    def src_node(self, source: str) -> dict:
        """音源子节点（live dict，读取/修改后需 set(source, node) 写回）。"""
        node = self.get(source)
        return node if isinstance(node, dict) else {}

    def src_api_base(self, source: str) -> str:
        """音源 API 地址；未配置时回落到内置默认地址。QQ 不依赖外部服务，恒返回 "local"。"""
        if source == "qq":
            return "local"
        configured = normalize_base(as_str(self.src_node(source).get("apiBase")))
        return configured or DEFAULT_API_BASE.get(source, "")

    # 旧版 QQ 节点用 credential/uin，与另两平台的 cookie/uid 不一致；
    # AstrBot 加载时会删掉 Schema 外键，导致扫码登录态每次重载即丢。保留读取别名做迁移。
    _LEGACY_KEYS = {"qq": {"cookie": "credential", "uid": "uin"}}

    def src_cookie(self, source: str) -> str:
        node = self.src_node(source)
        cookie = as_str(node.get("cookie")).strip()
        if cookie:
            return cookie
        legacy = self._LEGACY_KEYS.get(source, {}).get("cookie", "")
        return as_str(node.get(legacy)).strip() if legacy else ""

    def src_uid(self, source: str) -> str:
        node = self.src_node(source)
        uid = as_str(node.get("uid")).strip()
        if uid:
            return uid
        legacy = self._LEGACY_KEYS.get(source, {}).get("uid", "")
        return as_str(node.get(legacy)).strip() if legacy else ""

    def set_src_cookie(self, source: str, cookie: str, uid: str = "") -> None:
        node = dict(self.src_node(source))
        node["cookie"] = cookie
        if uid:
            node["uid"] = uid
        self.set(source, node)

    def clear_src_cookie(self, source: str) -> None:
        node = dict(self.src_node(source))
        node["cookie"] = ""
        node["uid"] = ""
        self.set(source, node)

    def src_quality(self, source: str) -> str:
        # 归一大小写：用户在 WebUI 填 "Lossless"/"HIRES" 时若不归一，
        # quality 阶梯查不到会静默降级到 lossless，音质降级无任何提示
        q = as_str(self.src_node(source).get("quality"), "auto").strip().lower()
        return q or "auto"

    def src_quality_unblock(self, source: str) -> bool:
        return as_bool(
            self.src_node(source).get("qualityUnblock")
            if source == "ncm"
            else self.src_node(source).get("trialFallback"),
            True,
        )

    def src_enabled(self, source: str) -> bool:
        """音源是否可用：QQ 始终可用（库内置），ncm/kg 需要配置 apiBase。"""
        if source == "qq":
            return True
        return bool(self.src_api_base(source))

    def enabled_sources(self) -> list[str]:
        return [s for s in SOURCES if self.src_enabled(s)]

    # ──────────── 黑白名单 ────────────
    @property
    def acl_mode(self) -> str:
        acl = self.get("acl")
        mode = as_str(acl.get("mode") if isinstance(acl, dict) else "", "off")
        return mode if mode in ("off", "blacklist", "whitelist") else "off"

    def acl_list(self, kind: str) -> list[str]:
        acl = self.get("acl")
        if not isinstance(acl, dict):
            return []
        return as_list(acl.get(kind))

    def set_acl(
        self, mode: str | None = None, blacklist: list | None = None, whitelist: list | None = None
    ) -> None:
        acl = dict(self.get("acl")) if isinstance(self.get("acl"), dict) else {}
        if mode is not None:
            acl["mode"] = mode
        if blacklist is not None:
            acl["blacklist"] = blacklist
        if whitelist is not None:
            acl["whitelist"] = whitelist
        self.set("acl", acl)

    # ──────────── WebUI ────────────
    @property
    def webui_enable(self) -> bool:
        return as_bool(self.webui_node().get("enable"), True)

    @property
    def webui_host(self) -> str:
        # 默认只听回环：面板能改全部插件配置、导入平台 Cookie，不该默认暴露到局域网
        return as_str(self.webui_node().get("host"), "127.0.0.1") or "127.0.0.1"

    @property
    def webui_host_allowlist(self) -> list[str]:
        """允许以域名访问面板的 Host 白名单（子域名自动放行）。"""
        raw = self.webui_node().get("hostAllowlist")
        if not isinstance(raw, list):
            return []
        return [d.strip().lower().lstrip(".") for d in (str(x) for x in raw) if d.strip()]

    @property
    def webui_port(self) -> int:
        return as_int(self.webui_node().get("port"), 17818, lo=1, hi=65535)

    @property
    def webui_password(self) -> str:
        return as_str(self.webui_node().get("password"), "")

    def set_webui_password(self, password: str) -> None:
        node = self.webui_node()
        node["password"] = password
        self.set("webui", node)

    def webui_node(self) -> dict:
        node = self.get("webui")
        return node if isinstance(node, dict) else {}

    # ──────────── 统计 ────────────
    @property
    def stats_enable(self) -> bool:
        return as_bool(self._stats().get("enable"), True)

    @property
    def stats_retention_days(self) -> int:
        return as_int(self._stats().get("retentionDays"), 30, lo=1, hi=365)

    def _stats(self) -> dict:
        node = self.get("stats")
        return node if isinstance(node, dict) else {}

    # ---- 汇总（设置卡片 / WebUI）----
    def toggle_items(self) -> list[dict]:
        return [
            {"name": "点歌", "on": self.enable and self.enable_song_request},
            {"name": "链接解析", "on": self.enable and self.enable_resolve},
            {"name": "语音发送", "on": self.send_vocal},
            {"name": "文件发送", "on": self.upload_file},
            {"name": "图片卡片", "on": self.render_list_card},
            {"name": "失败压缩重试", "on": self.ffmpeg_compress},
        ]

    # ──────────── 限速 / 冷却 / 定时 ────────────
    @property
    def rate_limit_ms(self) -> float:
        v = self.get("rateLimitMs")
        return as_int(v, 250, lo=0, hi=5000) if v is not None else 250.0

    @property
    def cooldown_sec(self) -> int:
        v = self.get("cooldownSec")
        return as_int(v, 0, lo=0, hi=600) if v is not None else 0

    @property
    def scheduler_enable(self) -> bool:
        return as_bool(self.sched_node().get("enable"), True)

    @property
    def scheduler_signin_hour(self) -> int:
        return as_int(self.sched_node().get("signinHour"), 8, lo=0, hi=23)

    @property
    def scheduler_ncm_signin(self) -> bool:
        return as_bool(self.sched_node().get("ncmSignin"), True)

    @property
    def scheduler_qq_refresh(self) -> bool:
        return as_bool(self.sched_node().get("qqRefresh"), True)

    def sched_node(self) -> dict:
        node = self.get("scheduler")
        return node if isinstance(node, dict) else {}
