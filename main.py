"""Music Hub 聚合点歌 —— AstrBot 插件入口。

QQ / 酷狗 / 网易云三平台聚合音乐：点歌、歌词、评论、榜单、扫码登录、
WebUI 管理面板（扫码 / 配置 / 统计 / 黑白名单）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core import PLUGIN_NAME
from .core.config import Config
from .core.errors import AclDeniedError, ApiError
from .core.logs import LogBuffer
from .core.service import MusicService
from .handlers import all_routes, install

PLUGIN_DIR = Path(__file__).resolve().parent
_log_buffer = LogBuffer()


class MusicHubPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = Config(config)
        self.service: MusicService | None = None
        self._webui = None
        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001 - 低版本 AstrBot 无 StarTools
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir
        self._tmpl_dir = PLUGIN_DIR / "resources" / "html"
        # 插件运行日志环形缓冲（WebUI「运行日志」页读取）。
        # logger 是进程级单例，重载插件会重新执行本模块 —— 先清掉旧缓冲再挂，
        # 否则 handler 越叠越多、日志条目成倍重复。
        plugin_logger = logging.getLogger(f"astrbot.plugin.{PLUGIN_NAME}")
        for stale in [h for h in plugin_logger.handlers if isinstance(h, LogBuffer)]:
            plugin_logger.removeHandler(stale)
        plugin_logger.addHandler(_log_buffer)

    async def initialize(self):
        self.service = MusicService(self, self.config, self._data_dir, self._tmpl_dir)
        self.service.log_buffer = _log_buffer
        await self.service.initialize()
        if self.config.webui_enable:
            try:
                await self._ensure_webui_password()
                from .core.webui import WebUIServer

                self._webui = WebUIServer(self.service)
                await self._webui.start()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[music_hub] WebUI 启动失败：{e}")
                self._webui = None

    async def _ensure_webui_password(self) -> None:
        """未设置密码时生成随机密码写回配置（不落日志）。

        Schema 默认值为空串；AstrBot 加载配置时会按 Schema 补默认值，因此新装的
        配置里 password 为空 —— 在这里生成，用户到 AstrBot 配置面板查看。
        """
        if self.config.webui_password:
            return
        import secrets

        self.config.set_webui_password(secrets.token_hex(8))
        await self.config.save_async()
        logger.info(
            "[music_hub] WebUI 首次启动已生成随机登录密码，请在 AstrBot 插件配置（webui.password）中查看"
        )

    # ──────────── LLM 函数工具（自然语言点歌） ────────────
    # 注意：工具不走聊天路由的闸门，这里要手动过 开关/黑白名单/冷却
    def _llm_guard(self, event: AstrMessageEvent, *, song_request: bool = False) -> str | None:
        """LLM 工具前置闸门：总开关（点歌类另受点歌开关）+ 黑白名单。返回拒绝文案或 None。"""
        if not self.service:
            return "音乐插件尚未初始化完成"
        reason = self.service.check_song_request() if song_request else self.service.check_playable()
        if reason:
            return f"功能不可用：{reason}"
        try:
            self.service.check_acl(event)
        except AclDeniedError:
            return "当前用户/群无权使用音乐功能"
        return None

    @filter.llm_tool(name="music_hub_play")
    async def llm_play(self, event: AstrMessageEvent, keyword: str, source: str = "auto") -> str:
        """点歌并播放一首歌。当用户想听歌、点歌、来一首歌时调用这个工具。

        Args:
            keyword(string): 歌名，可附歌手名（如 "晴天 周杰伦"）
            source(string): 音源，可选 ncm / kg / qq，不知道就填 auto
        """
        if err := self._llm_guard(event, song_request=True):
            return err
        if reason := self.service.check_cooldown(event):
            return reason
        try:
            src = source if source in ("ncm", "kg", "qq") else "auto"
            songs, real_src = await self.service.search_songs(keyword, src, limit=3)
            if not songs:
                self.service.release_cooldown(event)
                return f"没有搜到「{keyword}」相关的歌曲"
            result = await self.service.play_song(
                event, songs[0], source_label="AI 点歌", suppress_error_card=True
            )
            if result.get("ok"):
                return (
                    f"已播放：{songs[0].get('name')} - {songs[0].get('artist')}"
                    f"（{real_src} 音源，已发到当前会话）"
                )
            err = result.get("error") or result.get("reason") or ""
            # NotEnabledError 路径 result 没有 error 文案（play_song 已当面回复），别留悬空冒号
            return f"「{songs[0].get('name')}」播放失败" + (f"：{err}" if err else "")
        except Exception as e:  # noqa: BLE001
            # 工具入口盖了冷却章但没播出任何内容：无论异常类型都退章（对齐聊天路由的退章纪律）
            self.service.release_cooldown(event)
            if isinstance(e, ApiError):
                return e.user_msg()
            # 非 ApiError 原文可能带内部路径/URL，不交由模型转述给用户
            logger.warning(f"[music_hub] LLM 点歌异常: {e}")
            return "执行失败，详情见运行日志"

    @filter.llm_tool(name="music_hub_lyric")
    async def llm_lyric(self, event: AstrMessageEvent, keyword: str) -> str:
        """查询一首歌的歌词文本。用户想看歌词、问歌词内容时调用。

        Args:
            keyword(string): 歌名，可附歌手名（如 "晴天 周杰伦"）
        """
        if err := self._llm_guard(event):
            return err
        try:
            songs, _ = await self.service.search_songs(keyword, "auto", limit=1)
            if not songs:
                return f"没有搜到「{keyword}」相关的歌曲"
            lyric = await self.service.fetch_lyric(songs[0])
            from .core.api.qq import parse_lrc

            lines = parse_lrc(lyric.get("lrc", ""))[:40]
            if not lines:
                return f"「{songs[0].get('name')}」暂无歌词"
            return f"{songs[0].get('name')} - {songs[0].get('artist')} 的歌词（前 40 行）：\n" + "\n".join(
                lines
            )
        except Exception as e:  # noqa: BLE001
            # 工具没盖冷却章，无需退章；ApiError 给友好文案，其余异常细节只落日志
            if isinstance(e, ApiError):
                return e.user_msg()
            logger.warning(f"[music_hub] LLM 歌词查询异常: {e}")
            return "执行失败，详情见运行日志"

    @filter.llm_tool(name="music_hub_search")
    async def llm_search(self, event: AstrMessageEvent, keyword: str) -> str:
        """搜索歌曲，返回候选列表文本（不播放）。用户问"有没有XX歌"、"搜一下XX"时调用。

        Args:
            keyword(string): 歌名或关键词
        """
        if err := self._llm_guard(event):
            return err
        try:
            songs, src = await self.service.search_songs(keyword, "auto", limit=5)
            if not songs:
                return f"没有搜到「{keyword}」相关的歌曲"
            rows = [
                f"{s.get('index')}. {s.get('name')} - {s.get('artist')} [{s.get('source')}]" for s in songs
            ]
            return f"「{keyword}」搜索结果（{src}）：\n" + "\n".join(rows)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, ApiError):
                return e.user_msg()
            logger.warning(f"[music_hub] LLM 搜索异常: {e}")
            return "执行失败，详情见运行日志"

    async def terminate(self):
        if getattr(self, "_webui", None):
            try:
                await self._webui.stop()
            except Exception:  # noqa: BLE001
                pass
            self._webui = None
        if self.service:
            await self.service.terminate()
            self.service = None
        logging.getLogger(f"astrbot.plugin.{PLUGIN_NAME}").removeHandler(_log_buffer)


# 声明式路由在模块导入时安装（AstrBot 以 __module__+__name__ 为键绑定 handler，
# 必须先于星图扫描完成；重写 __module__ 以配合加载机制）
logger.info(f"[music_hub] 已注册 {install(MusicHubPlugin, filter, __name__, all_routes())} 个指令路由")
