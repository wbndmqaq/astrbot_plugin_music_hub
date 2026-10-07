"""搜索编排：单源 / 三源聚合、同名分组、分类型（WebUI）搜索、零结果建议。

依赖注入而非持有 service：显式传入 config / clients / stats 三个协作者，
core 层因此不再认识 core.service.MusicApp（原先 login/queue/scheduler/search/subs
五处反向引用 service，形成真实的循环依赖）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from astrbot.api import logger

from . import SOURCE_NAMES, SOURCES
from .errors import ApiError, NotEnabledError
from .matching import interleave, parse_source_hint

TAG = "[music_hub]"

# 聚合搜索时各源数量上限
AGGREGATE_PER_SOURCE = 6

# 分类型搜索（WebUI）：type → 各源 search 的 type 参数
MULTI_TYPE_PARAM = {
    "playlist": {"ncm": 1000, "kg": "special", "qq": "songlist"},
    "album": {"ncm": 10, "kg": "album", "qq": "album"},
    "artist": {"ncm": 100, "kg": "author", "qq": "singer"},
}

_NO_SOURCE_MSG = "没有可用音源：请先配置酷狗/网易云 API 地址（QQ音乐无需配置）"


class SearchResult:
    """一次搜索的产物：结果 + 本次搜索自己产生的可操作提示。

    提示随结果一起返回而不是挂在实例上：挂在实例上时 A 用户的搜索提示会被
    B 用户取走（用户看到与自己无关的「请扫码登录」，或自己的提示被吞）。
    """

    __slots__ = ("songs", "source", "notices")

    def __init__(self, songs: list[dict], source: str, notices: list[str] | None = None):
        self.songs = songs
        self.source = source
        self.notices = notices or []

    def as_tuple(self) -> tuple[list[dict], str]:
        return self.songs, self.source


class SongSearch:
    def __init__(self, config, clients: dict[str, Any], stats=None, enabled_sources=None):
        self._cfg = config
        self._clients = clients
        self._stats = stats
        # 音源可用列表：注册表随配置变化，注入成回调避免构造期固化
        self._enabled = enabled_sources or (lambda: [s for s in SOURCES if config.src_enabled(s)])

    # ──────────── 单源 / 聚合 ────────────
    async def search_full(self, keyword: str, source: str = "auto", limit: int | None = None) -> SearchResult:
        """搜索歌曲。source=auto 时三源并行聚合混排。"""
        limit = limit or self._cfg.max_list
        src, kw = parse_source_hint(keyword)
        if src != "auto":
            source = src
            keyword = kw
        if source != "auto":
            if source not in SOURCES:
                raise ApiError(f"未知音源 {source}", source=source)
            if not self._cfg.src_enabled(source):
                raise NotEnabledError(f"{SOURCE_NAMES[source]}音源未配置 API 地址", source=source)
            songs = await self._call(source, "search", self.one(source, keyword, limit), detail=keyword)
            return SearchResult(songs, source)
        enabled = self._enabled()
        if not enabled:
            raise ApiError(_NO_SOURCE_MSG, source="")
        tasks = [self.one(s, keyword, AGGREGATE_PER_SOURCE) for s in enabled]
        buckets, notices = await self._gather_sources(enabled, tasks, keyword, "search")
        return SearchResult(interleave(buckets)[:limit], "auto", notices)

    async def _call(self, source: str, action: str, coro, detail: str = ""):
        """执行并记账：失败记 ok=False 并把异常抛回调用方。"""
        try:
            out = await coro
        except Exception:
            self._record(source, action, False, detail)
            raise
        self._record(source, action, True, detail)
        return out

    def _record(self, source: str, action: str, ok: bool, detail: str) -> None:
        if self._stats is not None:
            self._stats.record(source, action, ok=ok, detail=detail)

    async def _gather_sources(
        self, sources: list[str], tasks: list, keyword: str, action: str
    ) -> tuple[list[list[dict]], list[str]]:
        """并发执行各源任务：失败的源记一条 ok=False 统计并置空桶，成功的记 ok=True。

        聚合语义要求"部分可用"——单个源失败不拖垮整体。但"需登录扫码"这类可操作错误
        会让用户困惑（只看到"没搜到"），因此把这类提示一并返回给调用方展示。
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        buckets: list[list[dict]] = []
        notices: list[str] = []
        for s, r in zip(sources, results, strict=False):
            if isinstance(r, BaseException):
                if isinstance(r, Exception) and not isinstance(r, NotEnabledError):
                    logger.warning(f"{TAG} 聚合搜索 {s} 失败: {type(r).__name__}: {r}")
                    # 只对 ApiError 调 user_msg —— 其它异常类型没有该方法，
                    # 误调抛 AttributeError 会顶替真实错误，排查时看到的不是真正原因
                    if isinstance(r, ApiError) and "登录" in str(r):
                        notices.append(f"{SOURCE_NAMES.get(s, s)}：{r.user_msg()}")
                self._record(s, action, False, keyword[:40])
                buckets.append([])
            else:
                self._record(s, action, True, keyword[:40])
                buckets.append(r)
        return buckets, notices

    async def one(self, source: str, keyword: str, limit: int) -> list[dict]:
        """单源搜索（换源重搜等编排场景直接复用）。"""
        return await self._clients[source].search(keyword, limit=limit)

    # ──────────── 同名分组 ────────────
    async def versions(self, keyword: str, limit: int | None = None) -> tuple[list[dict], str]:
        """聚合搜索并按「同名同歌手」分组 → 多音源选择列表。

        返回 (groups, source)。每个 group：
        {index, name, artist, album, cover, duration, dtMs,
         versions: [song…按默认音源优先排序], primary: song}
        """
        from .matching import group_versions

        limit = limit or self._cfg.max_list
        res = await self.search_full(keyword, "auto", AGGREGATE_PER_SOURCE)
        groups = group_versions(res.songs, preferred_first=self._cfg.default_source)
        return groups[:limit], res.source

    # ──────────── 分类型（WebUI 标签页）────────────
    async def typed(self, keyword: str, type_: str = "song", limit: int = 10) -> list[dict]:
        """非聚合的按类型搜索（playlist/album/artist），结果交错混排。"""
        enabled = self._enabled()
        if not enabled:
            raise ApiError(_NO_SOURCE_MSG, source="")
        tasks = [self._one_typed(s, keyword, type_, limit) for s in enabled]
        buckets, _notices = await self._gather_sources(enabled, tasks, keyword, f"search_{type_}")
        return interleave(buckets)

    async def _one_typed(self, source: str, keyword: str, type_: str, limit: int) -> list[dict]:
        client = self._clients[source]
        param = MULTI_TYPE_PARAM.get(type_, {}).get(source)
        if param is None:
            return []
        # 各平台的 type_ 形态不同（int / str / 枚举），由各客户端的适配层负责转换：
        # qq 客户端接受枚举名，ncm/kg 接受裸值。未知源直接返回空而不是走 else 分支。
        converter = getattr(client, "search_type", None)
        if callable(converter):
            param = converter(param)
            if param is None:
                return []
        return await client.search(keyword, limit=limit, type_=param)

    # ──────────── 零结果建议 ────────────
    async def suggest(self, keyword: str) -> list[str]:
        """零结果时的搜索建议（ncm/kg/qq 三源联想词）。"""
        out: list[str] = []
        for s in self._enabled():
            try:
                for it in await self._clients[s].suggest(keyword):
                    name = it.get("name") or ""
                    if name and name not in out:
                        out.append(name)
            except Exception:  # noqa: BLE001 - 建议属于锦上添花，失败静默
                continue
        return out[:6]
