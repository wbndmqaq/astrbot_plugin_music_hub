"""Music Hub 聚合点歌 —— AstrBot 插件入口。

QQ / 酷狗 / 网易云三平台聚合音乐：点歌、歌词、评论、榜单、扫码登录、
WebUI 管理面板（扫码 / 配置 / 统计 / 黑白名单）。
"""

from __future__ import annotations

import asyncio
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

# play_song 失败 reason → LLM 工具回传文案：内部枚举码与下载错误原文不交模型
# 转述（原文可能带直链，见 core.delivery 的 sanitize_message 口径）
_PLAY_REASON_LABELS = {
    "no_channel": "当前会话不支持语音或文件发送",
    "no_url": "未能获取播放链接",
    "download_fail": "音频下载失败，可稍后重试或换一首歌",
    "send_fail": "消息发送失败，可稍后再试",
}


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
        # 初始化窗口的去重集已完成使命（service 就绪后 handler 不再进入该分支）：
        # 置空释放窗口期积累的 umo 引用（规模有上界，纯卫生收口）
        scopes = getattr(type(self), "_init_notice_scopes", None)
        if scopes is not None:
            scopes.clear()
        # 明文口令迁移与面板开关解耦：禁用面板时配置里的明文 password 也要落成
        # 哈希——否则配置文件（含备份）持续持有明文口令，下次启用面板即生效
        try:
            await self._ensure_webui_password()
        except Exception as e:  # noqa: BLE001 - 迁移失败不阻断插件加载
            logger.error(f"[music_hub] WebUI 口令迁移失败：{e}")
        if self.config.webui_enable:
            try:
                self._warn_insecure_webui()
                from .core.webui import WebUIServer

                self._webui = WebUIServer(self.service)
                await self._webui.start()
            except Exception as e:  # noqa: BLE001
                logger.error(f"[music_hub] WebUI 启动失败：{e}")
                self._webui = None

    def _warn_insecure_webui(self) -> None:
        """非回环监听 + 未开 Secure Cookie 的组合在 http 直连时凭证明文过网，提醒一句。"""
        try:
            host = str(self.config.webui_host or "").lower()
            loopback = host in ("127.0.0.1", "::1", "localhost")
            if loopback or self.config.webui_secure_cookie:
                return
            # 端口从配置取：写死默认值的话，用户改过 webui.port 后提示失真
            logger.warning(
                "[music_hub] WebUI 监听在非回环地址且 secureCookie 未开启："
                "http 直连时登录密码与会话 Cookie 明文传输；仅经 https 反代访问可开启 "
                f"webui.secureCookie，或用防火墙限制 {self.config.webui_port} 端口的来源。裸暴露（无反代）"
                "还暴露在慢速连接的资源堆积风险下（aiohttp 对半截请求头无读取超时），"
                "公网可达时建议前置反向代理"
            )
        except Exception:  # noqa: BLE001 - 提醒失败不影响启动
            pass

    async def _ensure_webui_password(self) -> None:
        """未设置密码时生成随机密码；任何明文形式的口令在启动时迁移为 PBKDF2 哈希。

        配置里只存哈希：配置文件被同机其他进程读到也拿不到口令本身（面板可导出
        三平台登录态，明文落盘等于交出账号）。首启生成的随机口令写入独立收紧
        权限的告知文件——存哈希后配置面板无从「查看」，那是用户得知初始口令的
        唯一途径；登录改密后该文件自动删除。
        """
        from .core.webui._auth import hash_password_async, is_password_hash, write_initial_password_file

        pwd = str(self.config.webui_password or "")
        if pwd and is_password_hash(pwd):
            return
        import secrets

        if not pwd:
            # 128 bit 随机口令：登录口已有全局闸门限速，这里提高的是「永不更换的
            # 静态口令」的整体安全边际（成本为零）
            new_pwd = secrets.token_hex(16)
            self.config.set_webui_password(await hash_password_async(new_pwd))
            await self.config.save_async()
            # write_initial_password_file 内含 icacls 子进程（最坏 5s）+ 文件写入：
            # 同步调用会卡住整个事件循环（对齐 _server._ensure_auth 的 to_thread 口径）
            await asyncio.to_thread(write_initial_password_file, self._data_dir, new_pwd)
            logger.info(
                "[music_hub] WebUI 首次启动已生成随机登录密码，见插件数据目录下的 "
                "webui_initial_password.txt；登录后请修改密码（文件会自动删除）"
            )
        else:
            # 旧配置 / AstrBot 主面板写入的明文：迁移成哈希。用户自行设置的口令
            # 其本人知晓，无需告知文件
            self.config.set_webui_password(await hash_password_async(pwd))
            await self.config.save_async()
            logger.info("[music_hub] 已将 webui.password 迁移为哈希存储")

    # ──────────── LLM 函数工具（自然语言点歌） ────────────
    # 注意：工具不走聊天路由的闸门，这里要手动过 开关/黑白名单/冷却/频控
    def _llm_guard(self, event: AstrMessageEvent, *, song_request: bool = False) -> str | None:
        """LLM 工具前置闸门：总开关（点歌类另受点歌开关）+ 黑白名单 + browse 节流。

        聊天路由对直刷上游的指令挂 2s 轻频控（check_browse），LLM 工具入口不补上
        同款节流的话，模型被诱导连续调用时可绕过聊天端频控直刷上游。
        """
        if not self.service:
            return "音乐插件尚未初始化完成"
        reason = self.service.check_song_request() if song_request else self.service.check_playable()
        if reason:
            return f"功能不可用：{reason}"
        try:
            self.service.check_acl(event)
        except AclDeniedError:
            return "当前用户/群无权使用音乐功能"
        if reason := self.service.check_browse(event, 2.0):
            return reason
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
            # 内部 reason 枚举/下载错误原文不交给模型转述：枚举码模型会原样念出
            # （用户收到双份且第二份不可读），下载错误原文可能带直链——与下方
            # except 分支的脱敏纪律同口径，按 reason 查表给固定中文文案
            reason = str(result.get("reason") or "")
            detail = _PLAY_REASON_LABELS.get(reason)
            return f"「{songs[0].get('name')}」播放失败" + (f"：{detail}" if detail else "")
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
