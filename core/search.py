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

# 搜索/建议关键词长度上限：超长输入会原样打进上游 URL 与签名参数、并原样
# 回显到卡片文案。64 字符对任何真实歌名/歌手名都绰绰有余
MAX_KEYWORD_LEN = 64


def clip_keyword(kw: str) -> str:
    """入口统一截断：关键词超长时静默裁剪到上限（不报错——截断后仍能搜出结果）。"""
    return str(kw or "").strip()[:MAX_KEYWORD_LEN]


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
    def __init__(
        self, config, clients: dict[str, Any], stats=None, enabled_sources=None, missing_sources=None
    ):
        self._cfg = config
        self._clients = clients
        self._stats = stats
        # 音源可用列表：注册表随配置变化，注入成回调避免构造期固化
        self._enabled = enabled_sources or (lambda: [s for s in SOURCES if config.src_enabled(s)])
        # 配置开着但客户端不可用的音源（依赖缺失 / 初始化失败）：聚合搜索把它
        # 静默排除，用户会把「只剩两个源」误读成「别的源没这首歌」，需要提示兜底
        self._missing = missing_sources or (lambda: [])

    # ──────────── 单源 / 聚合 ────────────
    async def search_full(
        self, keyword: str, source: str = "auto", limit: int | None = None, *, per_source: int | None = None
    ) -> SearchResult:
        """搜索歌曲。source=auto 时三源并行聚合混排。

        per_source：聚合路径各源取数条数。默认 None 保持既有行为（每源取
        AGGREGATE_PER_SOURCE 条，并在分组前整体截断到 limit）；versions() 传
        limit 进来——每源多取、整体截断改由调用方分组后再做，否则交错序下
        limit 条只够拼出两三个分组。
        """
        limit = limit or self._cfg.max_list
        src, kw = parse_source_hint(keyword)
        if src != "auto":
            source = src
            keyword = kw
        keyword = clip_keyword(keyword)
        if source != "auto":
            if source not in SOURCES:
                raise ApiError(f"未知音源 {source}", source=source)
            if not self._cfg.src_enabled(source):
                raise NotEnabledError(f"{SOURCE_NAMES[source]}音源未配置 API 地址", source=source)
            songs = await self._call(source, "search", self.one(source, keyword, limit), detail=keyword)
            return SearchResult(songs, source)
        enabled = self._enabled()
        if not enabled and not self._missing():
            raise ApiError(_NO_SOURCE_MSG, source="")
        tasks = [self.one(s, keyword, max(AGGREGATE_PER_SOURCE, per_source or 0)) for s in enabled]
        buckets, notices, failed = await self._gather_sources(enabled, tasks, keyword, "search")
        merged = interleave(buckets)
        if per_source is None:
            merged = merged[:limit]
        # 配置开着但客户端缺位的源先提示（持久状态，每次搜索都该让用户知道
        # 结果里为什么少了一个源）；随后才是本次查询级失败
        missing = [s for s in self._missing() if s not in failed]
        if missing:
            names = " / ".join(SOURCE_NAMES.get(s, s) for s in missing)
            notices.append(f"{names} 音源不可用（依赖缺失或初始化失败），结果可能不全（详情见运行日志）")
        # 部分源失败必须让用户知道：只显示成功源的结果，用户会把「只剩 QQ」
        # 误读成「别的源没有这首歌」。失败信息进 notices（登录类提示在前）
        if failed:
            names = " / ".join(SOURCE_NAMES.get(s, s) for s in failed)
            notices.append(f"{names} 本次取数失败，以下结果可能不全（详情见运行日志）")
        return SearchResult(merged, "auto", notices)

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
    ) -> tuple[list[list[dict]], list[str], list[str]]:
        """并发执行各源任务：失败的源记一条 ok=False 统计并置空桶，成功的记 ok=True。

        聚合语义要求"部分可用"——单个源失败不拖垮整体。但"需登录扫码"这类可操作错误
        会让用户困惑（只看到"没搜到"），因此把这类提示一并返回给调用方展示；
        其余失败源以名字列表返回，由调用方决定提示形态。
        """
        results = await asyncio.gather(*tasks, return_exceptions=True)
        buckets: list[list[dict]] = []
        notices: list[str] = []
        failed: list[str] = []
        for s, r in zip(sources, results, strict=False):
            if isinstance(r, BaseException):
                if isinstance(r, Exception) and not isinstance(r, NotEnabledError):
                    # ApiError 的 __str__ 已含类型前缀（ApiError(ncm, code=..): msg），
                    # 再拼 type 名会双重前缀；其余异常类型才需要补类型名
                    if isinstance(r, ApiError):
                        logger.warning(f"{TAG} 聚合搜索 {s} 失败: {r}")
                    else:
                        logger.warning(f"{TAG} 聚合搜索 {s} 失败: {type(r).__name__}: {r}")
                    # 只对 ApiError 调 user_msg —— 其它异常类型没有该方法，
                    # 误调抛 AttributeError 会顶替真实错误，排查时看到的不是真正原因
                    if isinstance(r, ApiError) and "登录" in str(r):
                        notices.append(f"{SOURCE_NAMES.get(s, s)}：{r.user_msg()}")
                self._record(s, action, False, keyword[:40])
                buckets.append([])
                failed.append(s)
            else:
                self._record(s, action, True, keyword[:40])
                buckets.append(r)
        return buckets, notices, failed

    async def one(self, source: str, keyword: str, limit: int) -> list[dict]:
        """单源搜索（换源重搜等编排场景直接复用）。

        源不在 _clients 里（qq 库未装被注册表跳过 / 初始化失败被 create 跳过）时
        抛 NotEnabledError 而不是裸 KeyError——handler 侧只捕获 ApiError，
        KeyError 会逃过拦截一路炸到路由兜底，用户看到的不是可操作的提示。
        """
        client = self._clients.get(source)
        if client is None:
            raise NotEnabledError(
                f"{SOURCE_NAMES.get(source, source)}音源不可用（未安装依赖或初始化失败）", source=source
            )
        return await client.search(keyword, limit=limit)

    # ──────────── 同名分组 ────────────
    async def versions(self, keyword: str, limit: int | None = None) -> tuple[list[dict], str, list[str]]:
        """聚合搜索并按「同名同歌手」分组 → 多音源选择列表。

        返回 (groups, source, notices)。notices 是本次聚合取数收集的可操作提示
        （如「请扫码登录」），零结果时调用方应优先展示——否则用户只看到「没搜到」，
        不知道去扫码。每个 group：
        {index, name, artist, album, cover, duration, dtMs,
         versions: [song…按默认音源优先排序], primary: song}
        """
        from .matching import group_versions

        limit = limit or self._cfg.max_list
        # 每源取 max(AGGREGATE_PER_SOURCE, limit) 条、分组前不整体截断：
        # 交错序下 limit 条 ≈ 每源 limit/3，同名合并后只剩两三组
        res = await self.search_full(keyword, "auto", per_source=limit)
        groups = group_versions(res.songs, preferred_first=self._cfg.default_source)
        return groups[:limit], res.source, res.notices

    # ──────────── 分类型（WebUI 标签页）────────────
    async def typed(self, keyword: str, type_: str = "song", limit: int = 10) -> list[dict]:
        """非聚合的按类型搜索（playlist/album/artist），结果交错混排。"""
        keyword = clip_keyword(keyword)
        enabled = self._enabled()
        if not enabled:
            raise ApiError(_NO_SOURCE_MSG, source="")
        tasks = [self._one_typed(s, keyword, type_, limit) for s in enabled]
        buckets, _notices, failed = await self._gather_sources(enabled, tasks, keyword, f"search_{type_}")
        if failed:
            # 聚合路径把失败源拼进 notices 随结果展示；本路径返回纯列表
            # （service/webui 链路按列表消费），改返回结构牵连面大——至少留
            # 一条日志，让「列表莫名变短」在运行日志里可归因
            names = " / ".join(SOURCE_NAMES.get(s, s) for s in failed)
            logger.warning(f"{TAG} 分类型搜索 {names} 取数失败，以下结果可能不全")
        return interleave(buckets)

    async def _one_typed(self, source: str, keyword: str, type_: str, limit: int) -> list[dict]:
        client = self._clients.get(source)
        if client is None:  # 与 one() 同因：注册表跳过的源不能裸下标
            return []
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
        """零结果时的搜索建议（ncm/kg/qq 三源联想词）。

        三源并行取（串行时单个半死 API 服务最坏把 WebUI 联想接口拖住 3×20s）；
        失败的源静默跳过——建议属于锦上添花。"""
        keyword = clip_keyword(keyword)
        results = await asyncio.gather(
            *(self._suggest_one(s, keyword) for s in self._enabled()),
            return_exceptions=True,
        )
        out: list[str] = []
        for r in results:
            # BaseException 口径对齐 _gather_sources：CancelledError（3.8+ 非 Exception）
            # 在 return_exceptions=True 下会作为结果返回，按 Exception 判断会对它迭代炸 TypeError
            if isinstance(r, BaseException):
                continue
            for it in r:
                name = it.get("name") or ""
                if name and name not in out:
                    out.append(name)
        return out[:6]

    async def _suggest_one(self, source: str, keyword: str) -> list[dict]:
        # .get 而非下标：独立构造 SongSearch 时 fallback _enabled 不校验客户端
        # 存在性（依赖缺失的音源不在 _clients 里），下标会 KeyError
        client = self._clients.get(source)
        if client is None:
            return []
        return await client.suggest(keyword)
