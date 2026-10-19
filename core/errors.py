"""统一异常与错误文案。

ApiError 是所有音源 API 客户端的统一错误类型：``code`` 为业务/HTTP 错误码，
``source`` 标记音源，``user_msg()`` 产出面向聊天的友好文案。
"""

from __future__ import annotations

import re

from . import SOURCE_NAMES

# code=None 文案透传前的泄漏面剥除：上游 message 可能带内部地址（apiBase 指向的
# host:port、带 cookie query 的完整 URL）。插件自产的文案（搜索需登录、设备冷却、
# 操作指引等）不含这些形态，不受影响
_LEAK_RE = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.\-]*://\S+"  # 任何 scheme://...（含 query 里的 cookie）
    r"|(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?"  # IPv4[:port]
)


def sanitize_message(text) -> str:
    """剥除错误文本里的 URL 与 IPv4[:port]。

    ApiError.user_msg 内部同款口径；非 ApiError 异常（aiohttp 网络层/TLS 错误
    等，原文常含直链与主机名）要透给聊天端/WebUI/LLM 前也应过一遍，原文留日志。
    """
    return _LEAK_RE.sub("…", str(text or "")).strip()


# 网易云 (api-enhanced) 错误码 → 文案（唯一定义，ncm 客户端也从这里取）
NCM_ERRORS = {
    301: "未登录或登录已失效",
    400: "请求参数错误",
    402: "该歌曲需要 VIP",
    403: "请求被风控拒绝，可给 API 服务配置 realIP 或 randomCNIP",
    404: "歌曲无版权或不存在",
    406: "需要登录",
    460: "IP 风控，服务端需配置 randomCNIP",
    502: "上游接口调用失败",
    503: "调用过于频繁，稍后再试",
    1101: "登录已过期，请重新登录",
}

# 酷狗 (KuGouMusicApi) 错误码 → 文案（唯一定义，kg 客户端也从这里取）
KG_ERRORS = {
    152: "搜索需要登录：酷狗已禁止匿名搜索，请先扫码登录酷狗账号",
    20002: "权限受限",
    20006: "接口签名错误",
    20010: "需要登录",
    20017: "需要登录或 Token 失效",
    20018: "登录平台不匹配",
    20028: "触发风控：请求过快或设备异常，冷却后重试",
    20040: "设备信息异常：删除 device_cookies.json 后重启插件重试",
    35002: "需要登录",
    40007: "权限受限",
    55006: "房间不存在或参数不合法",
}

# QQ 音乐 (qqmusic_api) 错误码 → 文案（唯一定义，qq 客户端也从这里取）。
# 码的来源：qq.py 的 _NO_LOGIN_CODES 与取流归因注释、qqmusic_api 的 CGI_ERROR_MAP
QQ_ERRORS = {
    1000: "需要登录：请先扫码登录 QQ 音乐账号",
    2000: "请求被网关拒绝（需要签名）：请稍后再试",
    2001: "触发风控：请求过快，请稍后再试",
    104003: "该歌曲需要 QQ 音乐 VIP/登录才能获取完整链接",
    104004: "播放链接（VKey）获取失败：稍后重试通常可恢复",
    104013: "播放设备受限：该歌曲不允许在当前设备播放",
    104400: "QQ 登录凭证已失效，请重新扫码登录",
    104401: "QQ 登录凭证已失效，请重新扫码登录",
}


class ApiError(Exception):
    """音源 API 统一业务异常。"""

    def __init__(self, message: str, code=None, source: str = "", payload=None, timeout: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.source = source
        self.payload = payload if isinstance(payload, dict) else {}
        self.timeout = timeout

    def user_msg(self) -> str:
        """面向用户的文案：已知码给友好解释；未知码透传 message（插件自产的
        操作指引都在这里），但剥掉 URL/内网地址并限长——上游 message 可能带
        host:port 等内部地址（stats.py 归类注释同口径），不能原样直达聊天。"""
        if self.timeout:
            # 「检查 API 服务」只对自建 apiBase（ncm/kg）成立：QQ 是进程内直连，
            # 提示指向 API 服务反而误导
            if self.source == "qq":
                return "QQ 音乐请求超时，请稍后重试"
            return "请求超时，请检查 API 服务是否可用"
        if self.source == "ncm":
            table = NCM_ERRORS
        elif self.source == "kg":
            table = KG_ERRORS
        elif self.source == "qq":
            table = QQ_ERRORS
        else:
            table = {}
        if self.code is not None and self.code in table:
            return table[self.code]
        if self.code is not None:
            return f"音源返回异常（代码 {self.code}），详情见运行日志"
        return _LEAK_RE.sub("…", self.message).strip()[:120]

    def with_source(self) -> str:
        if not self.source:
            return self.user_msg()
        name = SOURCE_NAMES.get(self.source, self.source)
        return f"[{name}] {self.user_msg()}"

    def __str__(self) -> str:  # pragma: no cover - 调试友好
        return f"ApiError({self.source}, code={self.code}): {self.message}"


class NotEnabledError(ApiError):
    """音源未配置/未启用。"""


class AclDeniedError(Exception):
    """黑白名单拒绝。"""


class TooManyTasks(Exception):
    """后台任务并发超上限（拒绝新任务，避免内存被耗尽）。"""
