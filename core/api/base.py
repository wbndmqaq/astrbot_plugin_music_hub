"""音源客户端统一契约。

设计约束：
- 只声明 core 层真正调用到的方法（最小接口原则）。平台私有能力（kg.user_grade、
  qq.fav_songs、ncm.user_cloud）不进协议，走 ``PlatformExtras`` 由调用方 getattr 探测——
  否则三平台的私有方法会把协议撑成一个什么都有的巨型接口。
- 用 Protocol 而非 ABC：三平台实现里已有大量 @staticmethod 与惰性属性，
  ABC 会强制定义顺序且难以表达结构化子类型；Protocol + 启动期自检足够。

新增平台：实现本协议 → 在 registry 注册 → core/handlers 不再出现任何字面量分支。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# core 层统一调用的曲目相关方法（resolve_play / fetch_* 的唯一依赖面）
# 注意：song_detail 不在其中 —— 三家的签名真的不同（ncm 收 sid 列表返回列表、
# qq 收单个 sid 返回 dict|None、kg 没有该能力），强行统一只会逼出无意义的适配代码。
# 它属于平台私有能力，见 PlatformExtras。
_TRACK_METHODS = (
    "search",
    "suggest",
    "song_url_best",
    "song_lyric",
    "song_lyric_karaoke",
    "song_comments",
    "song_mv_url",
)
_ACCOUNT_METHODS = ("login_status", "vip_info")


@runtime_checkable
class SourceClient(Protocol):
    """core 层对音源客户端的全部要求。"""

    source: str
    """规范音源 id：'ncm' / 'kg' / 'qq'，与 core.constants.SOURCES 对齐。"""

    def ready(self) -> bool:
        """依赖是否就绪（如 qqmusic-api-python 是否安装）。"""
        ...

    async def search(self, keyword: str, limit: int = 10, *, type_: Any = None) -> list[dict]:
        """单曲搜索。type_ 的类型各平台不同（int / str / SearchType），故用 Any。"""
        ...

    async def suggest(self, keyword: str) -> list[dict]:
        """搜索联想词。无结果时返回空 list，不抛错。"""
        ...

    async def song_url_best(self, song: dict, preferred: str = "auto") -> dict:
        """输入归一化 song，输出 {url, quality, qualityLabel, trial, unblocked, ...}。

        失败抛 core.errors.ApiError（不放宽成 dict，调用方才能区分「没这首歌」
        与「这首歌暂时拿不到流」）。
        """
        ...

    async def song_lyric(self, song: dict) -> dict:
        """返回 {lrc, tlyric, yrc}；无歌词返回空串而非抛错。"""
        ...

    async def song_lyric_karaoke(self, song: dict) -> dict:
        """逐字歌词（KRC / QRC / YRC）。平台不支持时抛 ApiError，由上层回退到普通歌词。"""
        ...

    async def song_comments(self, song: dict, limit: int = 12) -> dict:
        """热门 + 最新评论。"""
        ...

    async def song_mv_url(self, song: dict) -> dict:
        """MV 直链；无 MV 时返回空 dict。"""
        ...

    async def login_status(self) -> dict:
        """当前登录态 {loggedIn, uid, nickname}。"""
        ...

    async def vip_info(self) -> dict:
        """会员信息。"""


@runtime_checkable
class PlatformExtras(Protocol):
    """平台私有能力：三家形态各异，经 getattr 探测后调用，不进核心契约。

    对应 kg.user_grade / qq.fav_songlist / ncm.user_cloud 这类只有单一平台支持的接口。
    core 层应写成「先探测再调用」，而不是硬编码 if source == SOURCE_KG。
    """

    async def song_detail(self, song: dict) -> dict | list[dict] | None:
        """曲目详情。**签名三平台不一致**（ncm 收 sid 列表、qq 收单个 sid、kg 无），
        调用方必须按 source 分派或先探测。"""
        ...


def missing_methods(client: Any) -> list[str]:
    """返回 client 未实现的协议方法名（空 list 表示契约完整）。

    启动期自检用：缺方法在这里暴露，而不是等用户点歌时的 AttributeError。
    """
    required = _TRACK_METHODS + _ACCOUNT_METHODS + ("ready",)
    missing = [m for m in required if not callable(getattr(client, m, None))]
    # source 是属性不是方法，单独查：缺了它错误文案的 with_source 会拿到空前缀
    if not getattr(client, "source", None):
        missing.append("source")
    return missing
