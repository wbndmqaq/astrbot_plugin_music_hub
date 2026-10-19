"""黑白名单（ACL）。

- ``off``：不限制
- ``blacklist``：命中名单（用户 ID 或群号）则拒绝
- ``whitelist``：仅名单内的用户/群可用

名单来自插件配置 ``acl.blacklist`` / ``acl.whitelist``（WebUI 可视化编辑），
条目为纯字符串：QQ 号、群号、或平台前缀形式 ``aiocqhttp:123456``。
"""

from __future__ import annotations

from astrbot.api import logger

from .config import Config
from .errors import AclDeniedError

TAG = "[music_hub]"


class Acl:
    def __init__(self, config: Config):
        self._config = config
        self._empty_warned = False

    def check(self, sender_id: str, group_id: str = "", platform: str = "") -> None:
        """校验，不通过抛 AclDeniedError。空 ID 一律放行（异常消息）。"""
        mode = self._config.acl_mode
        if mode == "off":
            return
        if not sender_id and not group_id:
            return
        # 平台名、候选与名单条目统一 strip+lower 归一：WebUI/手写配置里
        # "Aiocqhttp:123" 这类大写条目原样比对永远不命中；同理含大写字母的
        # 用户/群 ID（部分平台的 UUID/用户名形态）候选侧不同小写就配不上名单
        platform = str(platform or "").strip().lower()
        candidates = {
            str(sender_id or "").strip().lower(),
            str(group_id or "").strip().lower(),
        }
        if platform:
            if sender_id:
                candidates.add(f"{platform}:{str(sender_id).strip().lower()}")
            if group_id:
                candidates.add(f"{platform}:{str(group_id).strip().lower()}")
        candidates.discard("")
        if mode == "blacklist":
            blocked = self._config.acl_list("blacklist")
            if candidates & {b.strip().lower() for b in blocked}:
                raise AclDeniedError("当前用户/群已被加入黑名单")
        elif mode == "whitelist":
            allowed = {w.strip().lower() for w in self._config.acl_list("whitelist")}
            if not allowed:
                # 白名单模式配了空名单 = 一键全员自锁（含管理员），十有八九是
                # 误切模式。行为按 schema 文案保留，但至少给一条日志指路
                if not self._empty_warned:
                    self._empty_warned = True
                    logger.warning(
                        f"{TAG} ACL 为白名单模式但名单为空：所有用户/群（含管理员）都会被拒绝，"
                        "请在插件配置 acl.whitelist 填入名单或把 acl.mode 改回 off"
                    )
            if not (candidates & allowed):
                raise AclDeniedError("当前用户/群不在白名单内")
