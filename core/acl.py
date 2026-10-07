"""黑白名单（ACL）。

- ``off``：不限制
- ``blacklist``：命中名单（用户 ID 或群号）则拒绝
- ``whitelist``：仅名单内的用户/群可用

名单来自插件配置 ``acl.blacklist`` / ``acl.whitelist``（WebUI 可视化编辑），
条目为纯字符串：QQ 号、群号、或平台前缀形式 ``aiocqhttp:123456``。
"""

from __future__ import annotations

from .config import Config
from .errors import AclDeniedError


class Acl:
    def __init__(self, config: Config):
        self._config = config

    def check(self, sender_id: str, group_id: str = "", platform: str = "") -> None:
        """校验，不通过抛 AclDeniedError。空 ID 一律放行（异常消息）。"""
        mode = self._config.acl_mode
        if mode == "off":
            return
        if not sender_id and not group_id:
            return
        candidates = {str(sender_id or ""), str(group_id or "")}
        if platform:
            if sender_id:
                candidates.add(f"{platform}:{sender_id}")
            if group_id:
                candidates.add(f"{platform}:{group_id}")
        candidates.discard("")
        if mode == "blacklist":
            blocked = self._config.acl_list("blacklist")
            if candidates & {b.strip() for b in blocked}:
                raise AclDeniedError("当前用户/群已被加入黑名单")
        elif mode == "whitelist":
            allowed = {w.strip() for w in self._config.acl_list("whitelist")}
            if not (candidates & allowed):
                raise AclDeniedError("当前用户/群不在白名单内")
