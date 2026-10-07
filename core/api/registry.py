"""音源客户端注册表：source id → 工厂。

新增平台只需两步：
1. 在 core/constants.py 的 SOURCES 加常量；
2. 在本文件加一行 ``register("xx", XClient)``。

core / search / handlers 不再出现任何字面量 "ncm"/"kg"/"qq" 分支。
"""

from __future__ import annotations

from collections.abc import Callable

from astrbot.api import logger

from .base import SourceClient

ClientFactory = Callable[..., SourceClient]

_FACTORIES: dict[str, ClientFactory] = {}


def register(source: str, factory: ClientFactory) -> None:
    """注册音源工厂。重复注册视为编码错误（同名会静默覆盖导致排查困难）。"""
    if source in _FACTORIES:
        raise RuntimeError(f"音源 {source} 重复注册")
    _FACTORIES[source] = factory


def registered() -> list[str]:
    return list(_FACTORIES)


def create(config, **kwargs) -> dict[str, SourceClient]:
    """按注册表实例化全部音源。单个音源初始化失败只跳过，不影响其它平台。"""
    out: dict[str, SourceClient] = {}
    for src, factory in _FACTORIES.items():
        try:
            out[src] = factory(config, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[music_hub] 音源 {src} 初始化失败：{e}")
    return out
