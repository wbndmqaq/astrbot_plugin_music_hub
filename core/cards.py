"""卡片数据构造（聊天渲染层与 WebUI 共用的展示层）。

帮助卡 / 设置卡的数据也在这里组装 —— service 只提供配置与运行时数字，
展示结构归展示层。纯文本兜底在 :mod:`core.formatters`。
"""

from __future__ import annotations

from . import SOURCE_NAMES, SOURCES
from .help_data import VERSION
from .quality import quality_label


# ──────────── 卡片数据构造 ────────────
def build_list_card_data(
    keyword: str, songs: list[dict], source: str = "auto", *, tip: str = "", total: int | None = None
) -> dict:
    return {
        "keyword": keyword or "未知关键词",
        "total": total if total is not None else len(songs),
        "shown": len(songs),
        "source": source,
        "quality": "",
        "songs": [
            {
                "index": s.get("index", i + 1),
                "sid": s.get("sid", ""),
                "source": s.get("source", source),
                "sourceName": SOURCE_NAMES.get(s.get("source", ""), ""),
                "songName": s.get("name", ""),
                "singerName": s.get("artist", ""),
                "albumName": s.get("album", ""),
                "cover": s.get("cover", ""),
                "duration": s.get("duration", ""),
                "payplay": bool(s.get("pay")),
                "trial": bool(s.get("trial")),
            }
            for i, s in enumerate(songs)
        ],
        "tip": tip,
    }


def build_versions_card_data(keyword: str, groups: list[dict], *, tip: str = "") -> dict:
    """多音源选择列表卡：每行 = 一首（组）歌 + 可用音源版本 chips。"""
    return {
        "keyword": keyword or "未知关键词",
        "total": len(groups),
        "shown": len(groups),
        "source": "auto",
        "quality": "",
        "songs": [
            {
                "index": g.get("index", i + 1),
                "sid": (g.get("primary") or {}).get("sid", ""),
                "source": "auto",
                "sourceName": "",
                "songName": g.get("name", ""),
                "singerName": g.get("artist", ""),
                "albumName": g.get("album", ""),
                "cover": g.get("cover", ""),
                "duration": g.get("duration", ""),
                "payplay": not g.get("anyFree", True),
                "trial": False,
                "versions": [
                    {
                        "source": v.get("source", ""),
                        "sourceName": SOURCE_NAMES.get(v.get("source", ""), ""),
                        "pay": bool(v.get("pay")),
                    }
                    for v in g.get("versions", [])
                ],
            }
            for i, g in enumerate(groups)
        ],
        "tip": tip,
    }


def build_detail_card_data(song: dict, play: dict, source_label: str = "") -> dict:
    source = song.get("source", "")
    return {
        "source": source,
        "sourceName": SOURCE_NAMES.get(source, ""),
        "sourceLabel": source_label,
        "songName": song.get("name", ""),
        "singerName": song.get("artist", ""),
        "albumName": song.get("album", ""),
        "cover": song.get("cover", ""),
        "songId": song.get("sid2") or song.get("sid", ""),
        "duration": song.get("duration", ""),
        "qualityLabel": play.get("label") or play.get("qualityLabel") or "",
        "payplay": bool(song.get("pay")),
        "trial": bool(play.get("trial")),
        "error": play.get("error", ""),
        "tip": "",
    }


def build_lyric_card_data(
    song: dict, lyric: dict, lines: list[str], *, page: int = 1, page_total: int = 1, tip: str = ""
) -> dict:
    return {
        "source": song.get("source", ""),
        "sourceName": SOURCE_NAMES.get(song.get("source", ""), ""),
        "songName": song.get("name", ""),
        "singerName": song.get("artist", ""),
        "albumName": song.get("album", ""),
        "cover": song.get("cover", ""),
        "lines": lines,
        "lineCount": len(lines),
        "page": page,
        "pageTotal": page_total,
        "tip": tip,
    }


def build_comment_card_data(song: dict, comments: dict) -> dict:
    return {
        "source": song.get("source", ""),
        "sourceName": SOURCE_NAMES.get(song.get("source", ""), ""),
        "songName": song.get("name", ""),
        "singerName": song.get("artist", ""),
        "cover": song.get("cover", ""),
        "comments": [
            {
                "index": c.get("index", i + 1),
                "nick": c.get("nick", ""),
                "avatar": c.get("avatar", ""),
                "time": c.get("time", ""),
                "likes": c.get("likes", 0),
                "content": c.get("content", ""),
                "hot": bool(c.get("hot")),
            }
            # 条数与 formatters.format_comment_text 的文本兜底 [:10] 统一
            for i, c in enumerate(comments.get("hot", [])[:10])
        ],
        "total": comments.get("total", 0),
        "tip": "",
    }


def build_generic_card_data(
    title: str, subtitle: str, items: list[dict], *, source: str = "", tip: str = ""
) -> dict:
    return {
        "title": title,
        "subtitle": subtitle,
        "source": source,
        "sourceName": SOURCE_NAMES.get(source, ""),
        "total": len(items),
        "items": items,
        "tip": tip,
    }


def build_playlist_card_data(
    title: str, subtitle: str, playlists: list[dict], *, source: str = "", tip: str = ""
) -> dict:
    return {
        "title": title,
        "subtitle": subtitle,
        "source": source,
        "sourceName": SOURCE_NAMES.get(source, ""),
        "total": len(playlists),
        "totalPlay": "",
        "items": [
            {
                "index": p.get("index", i + 1),
                "name": p.get("name", ""),
                "creator": p.get("creator", ""),
                "cover": p.get("cover", ""),
                "trackCount": p.get("trackCount", 0),
                "playCountText": _fmt_count(p.get("playCount", 0)),
            }
            for i, p in enumerate(playlists)
        ],
        "tip": tip,
    }


def build_status_card_data(sources_status: list[dict]) -> dict:
    return {
        "title": "Music Hub 登录状态",
        "version": VERSION,
        "sources": sources_status,
        "tip": "扫码登录：发送 ncm登录 / kg登录 / qq登录，或打开 WebUI 扫码。",
    }


def build_help_data(
    *,
    stat_commands: int,
    stat_quality: str,
    stat_source: str,
    sections: list[dict],
    tip: str,
) -> dict:
    return {
        "version": VERSION,
        "statCommands": stat_commands,
        "statQuality": stat_quality,
        "statSource": stat_source,
        "tip": tip,
        "sections": sections,
    }


def _mask_base(url: str) -> str:
    """API 地址打码（设置卡展示用）：保留端口，IPv4 与域名各只露尾段。"""
    import re

    if not url:
        return "未配置"
    m = re.match(r"https?://([^/:]+)(:\d+)?", url)
    if not m:
        return "***"
    host = m.group(1)
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
        return "***.***.***.***" + (m.group(2) or "")
    parts = host.split(".")
    if len(parts) > 1:
        return f"***.{parts[-1]}" + (m.group(2) or "")
    return "***" + (m.group(2) or "")


def build_settings_data(cfg) -> dict:
    """设置卡数据（cfg：core.config.Config）。"""
    return {
        "title": "Music Hub 设置",
        "sources": [
            {
                "name": SOURCE_NAMES[s],
                "enabled": cfg.src_enabled(s),
                "apiBase": _mask_base(cfg.src_api_base(s)) if s != "qq" else "内置 qqmusic-api",
                "quality": quality_label(s, cfg.src_quality(s)),
                "logged": bool(cfg.src_uid(s)),
            }
            for s in SOURCES
        ],
        "maxList": cfg.max_list,
        "defaultSource": "聚合" if cfg.default_source == "auto" else SOURCE_NAMES.get(cfg.default_source, ""),
        "toggles": cfg.toggle_items(),
        "webui": {"enabled": cfg.webui_enable, "port": cfg.webui_port},
        "tip": "WebUI 支持扫码登录 / 配置 / 统计 / 黑白名单管理。",
    }


def _fmt_count(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 100000000:
        return f"{n / 100000000:.1f} 亿"
    if n >= 10000:
        return f"{n / 10000:.1f} 万"
    return str(n) if n else ""
