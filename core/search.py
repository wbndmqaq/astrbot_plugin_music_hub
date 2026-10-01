"""搜索编排：单源 / 三源聚合、同名分组、分类型（WebUI）搜索、零结果建议。

与 core.queue / core.subs 同构：Manager 持有 service 引用，service 以一行
委托保持公共 API（handlers / WebUI 只认 service）。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from . import SOURCE_NAMES, SOURCES
from .errors import ApiError, NotEnabledError
from .matching import interleave, parse_source_hint

TAG = "[music_hub]"

# 聚合搜索时各源数量上限
AGGREGATE_PER_SOURCE = 6

# 分类型搜索（WebUI）：type → 各源 search 的 type 参数
_MULTI_TYPE_PARAM = {
    "playlist": {"ncm": 1000, "kg": "special", "qq": "songlist"},
    "album": {"ncm": 10, "kg": "album", "qq": "album"},
    "artist": {"ncm": 100, "kg": "author", "qq": "singer"},
}

_NO_SOURCE_MSG = "没有可用音源：请先配置酷狗/网易云 API 地址（QQ音乐无需配置）"


class SearchManager:
    def __init__(self, service):
        self._service = service

    # ──────────── 单源 / 聚合 ────────────
    async def songs(
        self, keyword: str, source: str = "auto", limit: int | None = None
    ) -> tuple[list[dict], str]:
        """搜索歌曲。返回 (songs, 实际source)。auto 时三源并行聚合混排。"""
        limit = limit or self._service.config.max_list
        src, kw = parse_source_hint(keyword)
        if src != "auto":
            source = src
            keyword = kw
        if source != "auto":
            if source not in SOURCES:
                raise ApiError(f"未知音源 {source}", source=source)
            if not self._service.config.src_enabled(source):
                raise NotEnabledError(f"{SOURCE_NAMES[source]}音源未配置 API 地址", source=source)
            songs = await self._service.call(
                source, "search", self.one(source, keyword, limit), detail=keyword
            )
            return songs, source
        # 聚合
        enabled = self._service.enabled_sources()
        if not enabled:
            raise ApiError(_NO_SOURCE_MSG, source="")
        tasks = [self.one(s, keyword, AGGREGATE_PER_SOURCE) for s in enabled]
        buckets = await self._gather_sources(enabled, tasks, keyword, "search")
        return interleave(buckets)[:limit], "auto"

    async def _gather_sources(
        self, sources: list[str], tasks: list, keyword: str, action: str
    ) -> list[list[dict]]:
        """并发执行各源任务：失败的源记一条 ok=False 统计并置空桶，成功的记 ok=True。"""
        results = await asyncio.gather(*tasks, return_exceptions=True)
        buckets: list[list[dict]] = []
        for s, r in zip(sources, results, strict=False):
            if isinstance(r, BaseException):
                if isinstance(r, Exception) and not isinstance(r, NotEnabledError):
                    logger.warning(f"{TAG} 聚合搜索 {s} 失败: {r}")
                self._service.stats.record(s, action, ok=False, detail=keyword[:40])
                buckets.append([])
            else:
                self._service.stats.record(s, action, ok=True, detail=keyword[:40])
                buckets.append(r)
        return buckets

    async def one(self, source: str, keyword: str, limit: int) -> list[dict]:
        """单源搜索（换源重搜等编排场景直接复用）。"""
        return await self._service.client_of(source).search(keyword, limit=limit)

    # ──────────── 同名分组 ────────────
    async def versions(self, keyword: str, limit: int | None = None) -> tuple[list[dict], str]:
        """聚合搜索并按「同名同歌手」分组 → 多音源选择列表。

        返回 (groups, source)。每个 group：
        {index, name, artist, album, cover, duration, dtMs,
         versions: [song…按默认音源优先排序], primary: song}
        """
        from .matching import group_versions

        limit = limit or self._service.config.max_list
        songs, src = await self.songs(keyword, "auto", AGGREGATE_PER_SOURCE)
        groups = group_versions(songs, preferred_first=self._service.config.default_source)
        return groups[:limit], src

    # ──────────── 分类型（WebUI 标签页）────────────
    async def typed(self, keyword: str, type_: str = "song", limit: int = 10) -> list[dict]:
        """非聚合的按类型搜索（playlist/album/artist），结果交错混排。"""
        enabled = self._service.enabled_sources()
        if not enabled:
            raise ApiError(_NO_SOURCE_MSG, source="")
        tasks = [self._one_typed(s, keyword, type_, limit) for s in enabled]
        buckets = await self._gather_sources(enabled, tasks, keyword, f"search_{type_}")
        return interleave(buckets)

    async def _one_typed(self, source: str, keyword: str, type_: str, limit: int) -> list[dict]:
        client = self._service.client_of(source)
        param = _MULTI_TYPE_PARAM.get(type_, {}).get(source)
        if param is None:
            return []
        if source == "qq":
            from qqmusic_api.modules.search import SearchType

            st = {
                "songlist": SearchType.SONGLIST,
                "album": SearchType.ALBUM,
                "singer": SearchType.SINGER,
            }.get(param)
            if st is None:
                return []
            return await client.search(keyword, limit=limit, type_=st)
        return await client.search(keyword, limit=limit, type_=param)

    # ──────────── 零结果建议 ────────────
    async def suggest(self, keyword: str) -> list[str]:
        """零结果时的搜索建议（ncm/kg/qq 三源联想词）。"""
        out: list[str] = []
        for s in self._service.enabled_sources():
            try:
                for it in await self._service.client_of(s).suggest(keyword):
                    name = it.get("name") or ""
                    if name and name not in out:
                        out.append(name)
            except Exception:  # noqa: BLE001 - 建议属于锦上添花，失败静默
                continue
        return out[:6]
