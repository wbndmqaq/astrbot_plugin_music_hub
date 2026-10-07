"""MusicService：插件门面（编排层）。

所有 handler 只与本类交互。职责边界：
- 生命周期 / 统计包装 / 权限闸门 / 会话读写 —— 本类
- 会话注册表（umo）→ core.registry，播放历史与翻页缓存 → core.history，
  搜索编排 → core.search，展示数据构造 → core.cards，
  音源客户端 → core.api.*，投递 → core.delivery / core.remote
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain

from . import PLUGIN_NAME, SOURCE_KG, SOURCE_NAMES, SOURCE_NCM, SOURCES
from . import color as mh_color
from .acl import Acl
from .api import close_session, missing_methods
from .api import create as create_clients
from .cards import (
    build_detail_card_data,
    build_help_data,
    build_list_card_data,
    build_settings_data,
)
from .config import Config, as_int
from .delivery import deliver_song, schedule_cleanup
from .errors import ApiError, NotEnabledError, TooManyTasks
from .formatters import format_detail_text, format_list_text
from .help_data import HELP_SECTIONS, VERSION
from .history import HistoryStore
from .login import LoginFlows
from .matching import best_match, pick_version, version_names
from .media import cancel_cleanup_timers, get_temp_dir, startup_sweep
from .quality import quality_label, trial_suffix
from .queue import RequestDesk
from .ratelimit import limiter
from .registry import UmoRegistry
from .remote import send_audio_to
from .render import (
    Renderer,
    apply_theme,
    inject_theme_css,
    load_template,
    template_path,
    theme_bg,
)
from .scheduler import Scheduler
from .search import SongSearch
from .session import SessionStore, scope_of
from .stats import Stats
from .subs import Subscriptions

TAG = "[music_hub]"

# 听所有 串行上限
PLAY_ALL_LIMIT = 30
# 列表展示上限
LIST_SHOW_LIMIT = 30


class MusicService:
    def __init__(self, plugin, config: Config, data_dir: Path, tmpl_dir: Path):
        self.plugin = plugin
        self.config = config
        self.data_dir = data_dir
        self.tmpl_dir = tmpl_dir
        self.log = logger

        # 音源实例来自注册表：新增平台只要在 core/api 下实现 SourceClient 并 register，
        # 这里与 handlers 都不需要改。契约不完整的音源在启动日志里点名，不留到点歌时。
        # 契约检查直接复用 create 出的实例，不再为自检重复实例化一遍。
        clients = create_clients(
            config,
            # registry.create 把全部 kwargs 原样转发给每个工厂，参数名必须与各工厂
            # 声明的一一对应：酷狗与 QQ 的设备持久化格式互不兼容（酷狗是 dfid cookie，
            # QQ 是 qqmusic_api 指纹），共用一个文件会在每次重启时互相覆盖。
            kg_device_path=data_dir / "device_cookies.json",
            qq_device_path=data_dir / "qq_device.json",
        )
        for src, client in clients.items():
            missing = missing_methods(client)
            if missing:
                logger.warning(f"{TAG} 音源 {src} 未实现协议方法：{missing}")
        self.ncm = clients.get("ncm")
        self.kg = clients.get("kg")
        self.qq = clients.get("qq")
        self._clients = clients
        self.login = LoginFlows(self)
        self.sessions = SessionStore(plugin, data_dir)
        self.acl = Acl(config)
        self.scheduler = Scheduler(self)
        self.stats = Stats(data_dir, retention_days=config.stats_retention_days, enabled=config.stats_enable)
        self.queue = RequestDesk(self)
        self.subs = Subscriptions(self)

        # 浏览器运行时随实例生命周期：close() 会归位 disabled 状态，允许重载后重试
        self.renderer = Renderer()
        self._bg_tasks: set[asyncio.Task] = set()
        self._cooldowns: dict[str, float] = {}
        # 会话注册表（scope → umo）与播放历史 / 翻页缓存
        self.registry = UmoRegistry(self.get_kv, self.put_kv)
        self.history = HistoryStore()
        # 搜索编排（单源 / 聚合 / 分类型）：注入协作者而非反向持有 service
        self.search = SongSearch(config, clients, self.stats, self.enabled_sources)

    # ──────────── 生命周期 ────────────
    async def initialize(self) -> None:
        # kg 工厂失败时 self.kg 为 None（registry 会跳过并告警）：这里若裸调会把
        # 初始化失败放大成整个插件激活失败，违背单音源故障不拖垮其余音源的设计
        if self.kg is not None:
            await asyncio.to_thread(self.kg.load_device)
        limiter.update_interval(self.config.rate_limit_ms)
        await self.stats.start()
        await self.scheduler.start()
        await self.subs.load()
        await self.registry.load()
        self.spawn(self._startup())

    async def _startup(self) -> None:
        await startup_sweep()
        enabled = self.config.enabled_sources()
        logger.info(f"{TAG} 初始化完成，可用音源：{' / '.join(SOURCE_NAMES[s] for s in enabled) or '无'}")

    async def terminate(self) -> None:
        """逐步清理。每步独立兜底：任一步抛错都不能让后面的资源（Chromium、连接池、
        定时器）泄漏——插件重载时这些泄漏是累积性的，Windows 上表现为端口占用与 temp 膨胀。
        """

        async def _safe(label: str, coro) -> None:
            try:
                await coro
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 清理{label}失败：{e}")

        await _safe("点歌台", self.queue.stop_all())
        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        await _safe("临时文件", cancel_cleanup_timers())
        await _safe("调度器", self.scheduler.stop())
        await _safe("登录会话", self.login.gc())
        await _safe("播放历史", self.sessions.close())
        await _safe("统计", self.stats.close())
        # QQ 库未安装时 self.qq 为 None：_safe 的 coro 参数先求值，裸调 .close()
        # 会在兜底之外炸出 AttributeError，导致后面的 HTTP 会话与 Chromium 泄漏
        if self.qq is not None:
            await _safe("QQ 客户端", self.qq.close())
        await _safe("封面取色", mh_color.close())
        await _safe("HTTP 会话", close_session())
        await _safe("渲染器", self.renderer.close())
        logger.info(f"{TAG} 已退出")

    # 后台任务并发上限：超过则拒绝新任务，防止高频调用把内存耗尽
    MAX_BG_TASKS = 64

    def spawn(self, coro) -> asyncio.Task:
        """登记后台任务：terminate 时统一取消，避免重载后残留任务继续运行。

        超过 MAX_BG_TASKS 直接拒绝并关闭协程（未 await 的协程会告警），而不是无限累积。
        """
        if len(self._bg_tasks) >= self.MAX_BG_TASKS:
            coro.close()
            raise TooManyTasks(f"后台任务已达上限 {self.MAX_BG_TASKS}，请稍后再试")
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        # 必须消费异常：否则 KV 落盘、统计写盘等失败会被 asyncio 静默吞掉，
        # 表现为「数据莫名没存上」且日志无痕
        task.add_done_callback(self._log_task_error)
        return task

    @staticmethod
    def _log_task_error(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning(f"{TAG} 后台任务异常：{type(exc).__name__}: {exc}")

    def on_login_success(self, source: str) -> None:
        """登录成功回调（刷新特权缓存等）。"""
        logger.info(f"{TAG} {SOURCE_NAMES.get(source, source)} 登录成功")

    # ──────────── 基础 ────────────
    @property
    def context(self):
        return self.plugin.context

    def client_of(self, source: str):
        """按音源取客户端。查无此源（依赖缺失 / 初始化失败 / source 为空）时抛
        NotEnabledError 而不是裸 KeyError——后者会逃过 handler 侧的 ApiError
        拦截一路炸到路由兜底，用户看到的不是可操作的提示。"""
        client = self._clients.get(source)
        if client is None:
            raise NotEnabledError(
                f"{SOURCE_NAMES.get(source, source)}音源不可用（未安装依赖或初始化失败）", source=source
            )
        return client

    # ---- KV 轻封装（订阅表 / umo 持久化）----
    async def get_kv(self, key: str, default=None):
        try:
            return await self.plugin.get_kv_data(key, default)
        except Exception:  # noqa: BLE001
            return default

    async def put_kv(self, key: str, value) -> None:
        try:
            await self.plugin.put_kv_data(key, value)
        except Exception as e:  # noqa: BLE001
            # 吞掉但不无声：订阅表 / 注册表 / 调度标记落盘失败若无痕，
            # 表现为「数据莫名没存上」且排查无据
            self.log_warn(f"KV 写入失败（{key}）：{e}")

    # ---- 会话注册表（scope → umo，远程投递 / 点歌台 / 订阅推送的前置）----
    def note_umo(self, event) -> None:
        try:
            umo = event.unified_msg_origin
        except Exception:  # noqa: BLE001
            return
        if not umo:
            return
        if self.registry.note(self.scope(event), umo):
            self.spawn(self.registry.save())

    def umo_of(self, scope: str) -> str:
        return self.registry.umo_of(scope)

    def umo_rows(self) -> list[dict]:
        return self.registry.rows()

    # ---- 无事件发送（点歌台 / 订阅推送 / WebUI 远程投递）----
    async def send_to_scope(self, scope: str, text: str) -> None:
        umo = self.umo_of(scope)
        if umo:
            await self.send_to_umo(umo, text)

    async def send_to_umo(self, umo: str, text: str) -> bool:
        """无事件向指定会话发文本，返回是否送达（订阅推送据它决定 seen 是否落盘）。"""
        from .remote import send_text

        return await send_text(self, umo, text)

    async def send_audio(self, scope: str, song: dict, play: dict | None = None, *, note: str = "") -> dict:
        umo = self.umo_of(scope)
        if not umo:
            return {"ok": False, "reason": "no_umo"}
        return await send_audio_to(self, umo, song, play or {}, note=note)

    async def remote_play(self, umo: str, song: dict) -> dict:
        """WebUI 远程点歌：取流 → 主动发送到会话。"""
        result = {"ok": False, "reason": ""}
        try:
            play = await self.resolve_play(song)
            result = await send_audio_to(self, umo, song, play, note="WebUI 点歌")
        except NotEnabledError as e:
            await self.send_to_umo(umo, e.message)
            result = {"ok": False, "reason": "not_enabled"}
        except ApiError as e:
            await self.send_to_umo(umo, f"播放失败：{e.with_source()}")
            result = {"ok": False, "reason": "no_url", "error": e.user_msg()}
        except Exception as e:  # noqa: BLE001
            await self.send_to_umo(umo, f"播放失败：{e}")
            result = {"ok": False, "reason": "error", "error": str(e)}
        self.stats.record(
            song.get("source", ""), "play", ok=bool(result.get("ok")), detail=song.get("name", "")[:40]
        )
        return result

    # ---- 播放历史 / 翻页缓存（结构见 core.history，这里是转发层）----
    def history_of(self, scope: str) -> list[dict]:
        return self.history.of(scope)

    def history_all(self) -> list[dict]:
        return self.history.all(self.umo_of)

    def set_pager(self, scope: str, kind: str, data: dict) -> None:
        self.history.set_pager(scope, kind, data)

    def get_pager(self, scope: str, kind: str) -> dict | None:
        return self.history.get_pager(scope, kind)

    # ---- 分类型搜索（WebUI 标签页）----
    async def search_multi(self, keyword: str, type_: str = "song", limit: int = 10) -> list[dict]:
        return await self.search.typed(keyword, type_, limit)

    async def suggest_for(self, keyword: str) -> list[str]:
        return await self.search.suggest(keyword)

    def scope(self, event) -> str:
        try:
            return scope_of(
                event.get_platform_name(), event.get_group_id() or "", event.get_sender_id() or ""
            )
        except Exception:  # noqa: BLE001
            return "unknown"

    def plain(self, text: str) -> Plain:
        return Plain(text=text)

    async def reply(self, event, text: str) -> None:
        try:
            await event.send(event.make_result().message(text))
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"发送文本失败: {e}")

    async def send_chain(self, event, *components) -> None:
        """发送消息链；无媒体时失败只记日志，有媒体时上抛（由调用方降级）。"""
        chain = [c for c in components if c is not None]
        result = event.make_result()
        result.chain = chain
        has_media = any(not isinstance(c, Plain) for c in chain)
        try:
            await event.send(result)
        except Exception as e:  # noqa: BLE001
            if has_media:
                raise
            self.log_warn(f"发送消息失败: {e}")

    def log_warn(self, msg: str) -> None:
        logger.warning(f"{TAG} {msg}")

    def log_info(self, msg: str) -> None:
        logger.info(f"{TAG} {msg}")

    async def call(self, source: str, action: str, coro, *, detail: str = ""):
        """统计包装：记录 source/action 调用并透传结果/异常。

        成功记 detail（通常是关键词/歌名），失败记错误信息——WebUI 的
        「失败原因 TOP」按它聚合，混进歌名就没有意义了。
        """
        try:
            result = await coro
        except Exception as e:  # noqa: BLE001
            self.stats.record(source, action, ok=False, detail=str(e)[:60])
            raise
        self.stats.record(source, action, ok=True, detail=detail)
        return result

    # ---- VIP / 等级摘要（状态卡与 WebUI 账号页共用）----
    async def vip_summary(self, source: str) -> str:
        try:
            info = await self.client_of(source).vip_info()
        except Exception:  # noqa: BLE001
            return ""
        level = as_int((info or {}).get("vipLevel"), 0)
        if level <= 0:
            return ""
        expire = str((info or {}).get("expire") or "")
        return f"VIP{level}" + (f" · 到期 {expire[:10]}" if expire else "")

    async def grade_summary(self, source: str) -> str:
        """听歌等级摘要。仅酷狗提供该能力，其余平台返回空串。"""
        if source != SOURCE_KG:
            return ""
        try:
            g = await self.client_of(SOURCE_KG).user_grade()
        except Exception:  # noqa: BLE001
            return ""
        p = as_int((g or {}).get("p_grade"), 0)
        return f"听歌等级 {p}" if p > 0 else ""

    # ──────────── 权限 / 开关 ────────────
    def check_acl(self, event) -> None:
        try:
            sender = str(event.get_sender_id() or "")
            group = str(event.get_group_id() or "")
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            return
        self.acl.check(sender, group, platform)

    def check_playable(self) -> str | None:
        """返回拒绝原因；None = 放行。"""
        if not self.config.enable:
            return "插件已关闭"
        return None

    def check_song_request(self) -> str | None:
        if reason := self.check_playable():
            return reason
        if not self.config.enable_song_request:
            return "点歌功能已关闭"
        return None

    def check_cooldown(self, event) -> str | None:
        """同群点歌冷却；返回剩余提示或 None。通过时立即盖章（防同群并发连点），
        没播出任何内容的场景（零结果 / 取流失败）由 :meth:`release_cooldown` 退还。

        纯内存查表无 await 点，保持同步：调用方少一层 await，语义也更直白。"""
        sec = self.config.cooldown_sec
        if sec <= 0:
            return None
        scope = self.scope(event)
        now = time.monotonic()
        last = self._cooldowns.get(scope, 0.0)
        if now - last < sec:
            remain = int(sec - (now - last))
            return f"点歌冷却中，{remain} 秒后再试"
        self._cooldowns[scope] = now
        if len(self._cooldowns) > 256:
            # 按时间戳淘汰最旧的：dict 插入序 ≠ 最近使用（盖章只更新值不移动键），
            # 按插入序清会把最早盖章、往往也最活跃的群先清掉
            over = len(self._cooldowns) - 128
            for k in sorted(self._cooldowns, key=self._cooldowns.get)[:over]:
                self._cooldowns.pop(k, None)
        return None

    def release_cooldown(self, event) -> None:
        """本次触发没有产出任何播放（零结果 / 取流失败）时退还冷却，避免打错字也占冷却。"""
        self._cooldowns.pop(self.scope(event), None)

    # ──────────── 搜索 ────────────
    def enabled_sources(self) -> list[str]:
        """可用音源 = 配置里开着且客户端自检通过。

        走客户端的 ready() 而不是只看配置：契约不全或重依赖缺失的音源
        出现在这里就等于把「点歌失败」推迟到用户点击时才暴露。
        """
        return [s for s in self.config.enabled_sources() if self._is_ready(s)]

    def _is_ready(self, source: str) -> bool:
        client = self._clients.get(source)
        if client is None:
            return False
        ready = getattr(client, "ready", None)
        try:
            return bool(ready()) if callable(ready) else True
        except Exception:  # noqa: BLE001 - 自检异常按不可用处理
            return False

    async def search_songs(
        self, keyword: str, source: str = "auto", limit: int | None = None
    ) -> tuple[list[dict], str]:
        """搜索歌曲。返回 (songs, 实际source)。auto 时三源并行聚合混排。"""
        return (await self.search.search_full(keyword, source, limit)).as_tuple()

    async def search_with_notices(
        self, keyword: str, source: str = "auto", limit: int | None = None
    ) -> tuple[list[dict], str, list[str]]:
        """同 search_songs，但一并返回本次搜索产生的可操作提示（如「请扫码登录」）。

        提示随结果返回而不是挂在 search 实例上：挂实例上时并发搜索会互相取走对方的提示。
        """
        res = await self.search.search_full(keyword, source, limit)
        return res.songs, res.source, res.notices

    async def search_versions(
        self, keyword: str, limit: int | None = None
    ) -> tuple[list[dict], str, list[str]]:
        """聚合搜索并按「同名同歌手」分组 → 多音源选择列表。

        返回 (groups, source, notices)；notices 是聚合取数收集的登录提示，零结果时优先展示。
        """
        return await self.search.versions(keyword, limit)

    async def play_group(self, event, group: dict, *, source: str = "") -> dict:
        """播放一个多音源分组：source 为空用 primary（默认音源优先），否则用指定音源版本。

        取流失败自动换下一可用音源版本重试，全部失败才报错。
        """
        version = pick_version(group, source)
        if version is None:
            want = SOURCE_NAMES.get(source, source) if source else "任意"
            await self.reply(
                event, f"「{group.get('name', '')}」在 {want} 没有可用版本（可用：{version_names(group)}）"
            )
            return {"ok": False, "reason": "no_version"}
        result = await self.play_song(
            event,
            version,
            source_label="多音源" + (f" · {SOURCE_NAMES.get(version['source'], '')}" if source else ""),
            suppress_error_card=True,
        )
        if result.get("ok"):
            return result
        if result.get("reason") not in ("no_url", "download_fail"):
            return result
        tried = [version.get("source", "")]
        last_err = result.get("error", "")
        for v in group.get("versions", []):
            if v is version or v.get("source") in tried:
                continue
            tried.append(v.get("source", ""))
            result = await self.play_song(
                event,
                v,
                source_label=f"自动换源 · {SOURCE_NAMES.get(v.get('source'), '')}",
                suppress_error_card=True,
            )
            if result.get("ok"):
                return result
            last_err = result.get("error", "") or last_err
        await self.reply(
            event,
            f"「{group.get('name', '')}」在 {' / '.join(SOURCE_NAMES.get(s, s) for s in tried)} 都取流失败"
            + (f"：{last_err}" if last_err else ""),
        )
        return {"ok": False, "reason": "no_url"}

    async def switch_source(self, event, source: str) -> None:
        """换源重放：把最近播放的歌在指定音源重新搜索并播放。"""
        scope = self.scope(event)
        entry = await self.sessions.get(scope)
        last = (entry or {}).get("data", {}).get("lastPlayed") if entry else None
        if not last:
            self.release_cooldown(event)
            await self.reply(event, "还没有播放过歌曲，先「点歌」一次吧")
            return
        kw = f"{last.get('name', '')} {last.get('artist', '')}".strip()
        songs = await self.call(source, "search", self.search.one(source, kw, 5), detail=kw)
        if not songs:
            self.release_cooldown(event)
            await self.reply(event, f"{SOURCE_NAMES.get(source, source)}没有找到「{kw}」")
            return
        target = best_match(songs, last.get("name", ""), last.get("artist", ""))
        await self.play_song(event, target, source_label="换源")

    async def switch_source_for(self, event, song: dict, source: str) -> None:
        """把指定歌曲在另一音源重找并播放（听N 音源 用）。"""
        if song.get("source") == source:
            await self.play_song(event, song)
            return
        kw = f"{song.get('name', '')} {song.get('artist', '')}".strip()
        songs = await self.call(source, "search", self.search.one(source, kw, 5), detail=kw)
        if not songs:
            self.release_cooldown(event)
            await self.reply(event, f"{SOURCE_NAMES.get(source, source)}没有找到「{kw}」")
            return
        target = best_match(songs, song.get("name", ""), song.get("artist", ""))
        await self.play_song(event, target, source_label="指定音源")

    # ──────────── 取流 ────────────
    async def resolve_play(self, song: dict) -> dict:
        """按音源取播放链接（音质阶梯 / 试听 / 解灰兜底）。

        三个音源客户端遵循同一协议（song dict 进、play dict 出），新增平台无需改这里。
        """
        source = song.get("source", "")
        client = self.client_of(source)
        preferred = self.config.src_quality(source)
        play = await self.call(source, "url", client.song_url_best(song, preferred))
        play.setdefault("qualityLabel", quality_label(source, play.get("quality", "")))
        play["label"] = trial_suffix(play)
        play["source"] = source
        return play

    # ──────────── 播放 ────────────
    _LAST_PLAYED_KEYS = (
        "source",
        "sid",
        "sid2",
        "name",
        "artist",
        "album",
        "cover",
        "duration",
        "dtMs",
        "pay",
        "trial",
        "mvid",
    )

    async def _remember_last_played(self, event, song: dict) -> None:
        """记住本次播放，供「换源」/「红心」使用。"""
        scope = self.scope(event)
        entry = await self.sessions.get(scope)
        if entry:
            data = dict(entry.get("data", {}))
            data["lastPlayed"] = {k: song.get(k) for k in self._LAST_PLAYED_KEYS}
            await self.sessions.set(scope, entry.get("kind", "songs"), data)

    async def play_song(
        self, event, song: dict, *, source_label: str = "", suppress_error_card: bool = False
    ) -> dict:
        """完整播放流程：取流 → 详情卡 → 投递。

        suppress_error_card：取流失败时不发错误详情卡（自动换源/队列等调用方
        会自行汇总报错），失败原因随返回值带回。
        """
        source = song.get("source", "")
        try:
            play = await self.resolve_play(song)
        except NotEnabledError as e:
            await self.reply(event, e.message)
            self.release_cooldown(event)
            return {"ok": False, "reason": "not_enabled"}
        except ApiError as e:
            self.stats.record(source, "play", ok=False, detail=str(e)[:60])
            self.release_cooldown(event)
            if suppress_error_card:
                return {"ok": False, "reason": "no_url", "error": e.with_source()}
            # 取流失败也出一张详情卡，带原因
            await self.reply_detail_with_error(event, song, e, source_label)
            return {"ok": False, "reason": "no_url"}

        await self._remember_last_played(event, song)

        # 详情卡（含封面）
        card_sent = False
        if self.config.render_list_card:
            try:
                await self.reply_detail_card(event, song, play, source_label)
                card_sent = True
            except Exception as e:  # noqa: BLE001
                self.log_warn(f"详情卡渲染失败: {e}")

        result = await deliver_song(self, event, song, play, options={"skipTextInfo": card_sent})
        if result.get("ok"):
            self.history.record(self.scope(event), song)
        self.stats.record(source, "play", ok=bool(result.get("ok")), detail=song.get("name", "")[:40])
        return result

    async def play_all(self, event, songs: list[dict]) -> dict:
        """串行播放整个列表。

        聚合点歌存进会话的是分组 dict（带 versions 键、无顶层 source/sid），
        直接喂给 play_song 会在 client_of("") 上炸 KeyError：遍历前先归一化到
        可播放的版本条目。"""
        fixed = []
        for song in songs:
            if song.get("versions"):
                song = song.get("primary") or song["versions"][0]
            fixed.append(song)
        ok, fail = 0, 0
        total = min(len(fixed), PLAY_ALL_LIMIT)
        await self.reply(event, f"开始连播 {total} 首（最多 {PLAY_ALL_LIMIT} 首）")
        for i, song in enumerate(fixed[:PLAY_ALL_LIMIT]):
            try:
                result = await self.play_song(event, song, source_label="连播")
                if result.get("ok"):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:  # noqa: BLE001
                self.log_warn(f"连播第 {i + 1} 首失败: {e}")
                fail += 1
            if i < total - 1:
                await asyncio.sleep(1)
        return {"ok": ok, "fail": fail}

    # ──────────── 会话 ────────────
    async def list_to_session(
        self,
        event,
        keyword: str,
        songs: list[dict],
        *,
        source: str = "auto",
        kind: str = "songs",
        action: str = "",
        tip: str = "",
    ) -> None:
        """存会话 + 渲染列表卡（或文本兜底）。"""
        scope = self.scope(event)
        await self.sessions.set(scope, kind, {"keyword": keyword, "songs": songs}, action=action)
        shown = songs[:LIST_SHOW_LIMIT]
        data = build_list_card_data(keyword, shown, source, tip=tip, total=len(songs))
        await self.reply_card_or_text(event, data, "list", source, format_list_text)

    async def start_select(self, event, keyword: str, action: str, action_label: str) -> None:
        """二段式：带关键词搜索并记录 action；不带关键词复用当前列表。"""
        scope = self.scope(event)
        if keyword:
            songs, src = await self.search_songs(keyword)
            if not songs:
                await self.reply(event, f"没有搜到「{keyword}」相关的歌曲")
                return
            await self.sessions.set(scope, "songs", {"keyword": keyword, "songs": songs}, action=action)
            data = build_list_card_data(
                keyword, songs[:LIST_SHOW_LIMIT], src, tip=f"回复 听N 查看{action_label}"
            )
            await self.reply_card_or_text(event, data, "list", src, format_list_text)
        else:
            songs = await self.sessions.songs_of(scope)
            if not songs:
                await self.reply(
                    event, f"当前没有歌曲列表，请先「点歌 关键词」或带关键词使用，如「{action_label} 晴天」"
                )
                return
            await self.sessions.update_action(scope, action)
            src = songs[0].get("source", "auto") if songs else "auto"
            await self.reply(event, f"回复 听N 查看{action_label}（列表共 {len(songs)} 首）")

    async def take_action_target(self, event, n: int) -> tuple[dict | None, str]:
        """听N 的会话消费：返回 (song, pending_action) 并清除 action。"""
        scope = self.scope(event)
        entry = await self.sessions.get(scope, refresh=True)
        if not entry:
            return None, ""
        songs = entry.get("data", {}).get("songs") or []
        if not songs or not (1 <= n <= len(songs)):
            return None, entry.get("action", "")
        song = songs[n - 1]
        action = entry.get("action", "")
        if action:
            await self.sessions.clear_action(scope)
        # 无 action 时不做整包回写：get(refresh=True) 已续过内存 TTL，KV 不回写是
        # session.py 声明过的可接受近似（跨重载剩余 TTL 偏短）。原先这里的 set 与
        # 上面 get 之间没有锁，连点「听N」+「点歌 新词」交错时，旧 entry 的回写会
        # 把刚写入的新列表顶掉（缓存与 KV 双写竞态）。
        return song, action

    # ──────────── 渲染 ────────────
    async def render_card(self, data: dict, tpl_name: str, source: str = "") -> str | None:
        """渲染模板 → PNG 路径；失败返回 None（调用方走文本兜底）。"""
        if not self.config.render_list_card:
            return None
        if self.renderer.disabled:
            return None
        await apply_theme(data)
        tpl_path = template_path(self.tmpl_dir, tpl_name)
        try:
            template = await load_template(tpl_path)
            data = dict(data)
            data.setdefault("version", VERSION)
            # Jinja 渲染是同步 CPU 操作，放线程池避免大模板渲染卡住事件循环
            html = await asyncio.to_thread(template.render, data=data)
            html = inject_theme_css(html, data, tpl_name, source or data.get("source", ""))
            out = os.path.join(
                await get_temp_dir(), f"mh_{tpl_name}_{int(time.time() * 1000)}_{os.urandom(3).hex()}.png"
            )
            bg = theme_bg(data, source or data.get("source", ""))
            ok = await self.renderer.render(html, out, bg)
            if ok:
                self.schedule_unlink(out, 120)
                return out
        except FileNotFoundError:
            self.log_warn(f"模板缺失: {tpl_name}")
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"渲染 {tpl_name} 失败: {e}")
        return None

    async def reply_card_or_text(self, event, data: dict, tpl_name: str, source: str, text_fn) -> None:
        """渲染卡片发送；平台不支持本地图（如钉钉）或渲染失败时回退纯文本。"""
        from .platform_caps import caps_of

        path = None
        if caps_of(event).local_image:
            path = await self.render_card(data, tpl_name, source)
        if path:
            try:
                await self.send_chain(event, Image.fromFileSystem(path))
                return
            except Exception as e:  # noqa: BLE001
                self.log_warn(f"卡片发送失败，回退文本: {e}")
        try:
            await self.reply(event, text_fn(data))
        except Exception as e:  # noqa: BLE001
            self.log_warn(f"文本兜底也失败: {e}")

    async def reply_detail_card(self, event, song: dict, play: dict, source_label: str = "") -> None:
        data = build_detail_card_data(song, play, source_label)
        await self.reply_card_or_text(event, data, "detail", song.get("source", ""), format_detail_text)

    async def reply_detail_with_error(self, event, song: dict, err: ApiError, source_label: str = "") -> None:
        play = {"url": "", "label": "", "qualityLabel": "", "error": err.with_source()}
        data = build_detail_card_data(song, play, source_label)
        await self.reply_card_or_text(event, data, "detail", song.get("source", ""), format_detail_text)

    def schedule_unlink(self, path: str, sec: int = 120) -> None:
        with contextlib.suppress(Exception):
            schedule_cleanup(path, sec)

    # ──────────── 歌词 / 评论（per-source 细节收进各客户端 song_* 协议） ────────────
    async def fetch_lyric(self, song: dict) -> dict:
        """普通歌词（LRC）。"""
        source = song.get("source", "")
        return await self.call(
            source, "lyric", self.client_of(source).song_lyric(song), detail=song.get("name", "")[:40]
        )

    async def fetch_lyric_karaoke(self, song: dict) -> dict:
        """逐字歌词（KRC/QRC/YRC）；空结果或源不支持时回退普通歌词。"""
        source = song.get("source", "")
        try:
            data = await self.call(
                source,
                "lyric_karaoke",
                self.client_of(source).song_lyric_karaoke(song),
                detail=song.get("name", "")[:40],
            )
        except ApiError:
            return await self.fetch_lyric(song)
        if data.get("yrc"):
            return data
        return await self.fetch_lyric(song)

    # ──────────── 评论 ────────────
    async def fetch_comments(self, song: dict, limit: int = 12) -> dict:
        source = song.get("source", "")
        return await self.call(
            source,
            "comment",
            self.client_of(source).song_comments(song, limit),
            detail=song.get("name", "")[:40],
        )

    # ──────────── MV ────────────
    async def fetch_mv_url(self, song: dict) -> dict:
        source = song.get("source", "")
        return await self.call(source, "mv", self.client_of(source).song_mv_url(song))

    # ──────────── 网易歌单便捷 ────────────
    async def ncm_playlist_songs(self, pid: str) -> tuple[dict, list[dict]]:
        ncm = self.client_of(SOURCE_NCM)
        pl = await ncm.playlist_detail(pid)
        songs = await ncm.playlist_songs(pid, LIST_SHOW_LIMIT)
        return pl, songs

    # ──────────── 帮助 / 设置数据（展示结构在 core.cards）────────────
    def help_data(self) -> dict:
        # 统计已注册路由数。astrbot.core 是内部路径（升级可能变），拿不到就退回
        # 帮助卡实际列出的条目数——报一个编出来的数比报错更糟。
        route_count = 0
        try:
            from astrbot.core.star.star_handler import star_handlers_registry

            # handler_module_path 形如 data.plugins.<插件目录>.main，按插件名匹配
            route_count = sum(
                1 for h in star_handlers_registry if PLUGIN_NAME in getattr(h, "handler_module_path", "")
            )
        except Exception:  # noqa: BLE001
            route_count = 0
        if route_count <= 0:
            route_count = sum(len(sec.get("items", [])) for sec in HELP_SECTIONS)
        return build_help_data(
            stat_commands=route_count,
            stat_quality=" / ".join(self.config.src_quality(s) for s in SOURCES),
            stat_source=f"{len(self.enabled_sources())} 平台",
            sections=HELP_SECTIONS,
            tip="回复 听N 播放列表中的歌曲；ncm:/kg:/qq: 前缀可指定音源。",
        )

    def settings_data(self) -> dict:
        return build_settings_data(self.config)
