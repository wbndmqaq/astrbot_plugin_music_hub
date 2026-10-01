"""统一异常与错误文案。

ApiError 是所有音源 API 客户端的统一错误类型：``code`` 为业务/HTTP 错误码，
``source`` 标记音源，``user_msg()`` 产出面向聊天的友好文案。
"""

from __future__ import annotations

from . import SOURCE_NAMES

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
        """面向用户的文案：已知码给友好解释，否则给原始信息。"""
        if self.timeout:
            return "请求超时，请检查 API 服务是否可用"
        table = NCM_ERRORS if self.source == "ncm" else KG_ERRORS if self.source == "kg" else {}
        if self.code is not None and self.code in table:
            return table[self.code]
        return f"{self.message}"

    def with_source(self) -> str:
        name = SOURCE_NAMES.get(self.source, self.source or "音源")
        return f"[{name}] {self.user_msg()}"

    def __str__(self) -> str:  # pragma: no cover - 调试友好
        return f"ApiError({self.source}, code={self.code}): {self.message}"


class NotEnabledError(ApiError):
    """音源未配置/未启用。"""


class AclDeniedError(Exception):
    """黑白名单拒绝。"""
