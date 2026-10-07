"""纯文本兜底格式化：卡片渲染不可用时（未装 Playwright / 平台不支持本地图）降级用。

与 :mod:`core.cards` 的 ``build_*`` 成对：那边组装展示数据，这里转成聊天文本。
"""

from __future__ import annotations


def format_versions_text(data: dict) -> str:
    lines = [f"♪ 点歌「{data.get('keyword', '')}」 共 {data.get('total', 0)} 首"]
    for s in data.get("songs", []):
        srcs = "".join(
            {"ncm": "[网]", "kg": "[狗]", "qq": "[Q]"}.get(v.get("source"), "") for v in s.get("versions", [])
        )
        pay = " [VIP]" if s.get("payplay") else ""
        lines.append(f"{s.get('index')}. {s.get('songName')} - {s.get('singerName')} {srcs}{pay}")
    if data.get("tip"):
        lines.append(f"提示：{data['tip']}")
    lines.append("回复 听N 播放（听N qq 可指定音源）")
    return "\n".join(lines)


# ──────────── 纯文本兜底 ────────────
def format_list_text(data: dict) -> str:
    lines = [f"♪ 点歌「{data.get('keyword', '')}」 共 {data.get('total', 0)} 首"]
    for s in data.get("songs", []):
        pay = " [VIP]" if s.get("payplay") else ""
        src = f" [{s.get('sourceName', '')}]" if data.get("source") == "auto" else ""
        lines.append(f"{s.get('index')}. {s.get('songName')} - {s.get('singerName')}{pay}{src}")
    if data.get("tip"):
        lines.append(f"提示：{data['tip']}")
    lines.append("回复 听N 播放")
    return "\n".join(lines)


def format_detail_text(data: dict) -> str:
    lines = [
        f"♪ {data.get('songName')} - {data.get('singerName')}",
        f"专辑：{data.get('albumName')}" if data.get("albumName") else "",
        f"音质：{data.get('qualityLabel')}" if data.get("qualityLabel") else "",
        f"[{data.get('sourceName', '')}]",
    ]
    if data.get("error"):
        lines.append(f"⚠ {data['error']}")
    return "\n".join(x for x in lines if x)


def format_generic_text(data: dict) -> str:
    lines = [f"♪ {data.get('title', '')}（共 {data.get('total', 0)} 项）"]
    for it in data.get("items", []):
        sub = f" - {it['sub']}" if it.get("sub") else ""
        lines.append(f"{it.get('index')}. {it.get('name', '')}{sub}")
    if data.get("tip"):
        lines.append(f"提示：{data['tip']}")
    return "\n".join(lines)


def format_playlist_text(data: dict) -> str:
    lines = [f"♪ {data.get('title', '')}（共 {data.get('total', 0)} 个歌单）"]
    for it in data.get("items", []):
        lines.append(
            f"{it.get('index')}. {it.get('name', '')}（{it.get('trackCount', 0)} 首 · {it.get('playCountText', '')}）"
        )
    if data.get("tip"):
        lines.append(f"提示：{data['tip']}")
    return "\n".join(lines)


def format_lyric_text(song: dict, lines: list[str]) -> str:
    head = f"♪ {song.get('name')} - {song.get('artist')}\n"
    return head + "\n".join(lines[:50])


def format_comment_text(song: dict, comments: dict) -> str:
    lines = [f"♪ {song.get('name')} 热门评论"]
    for c in comments.get("hot", [])[:10]:
        lines.append(
            f"{c.get('index')}. [{c.get('nick')}] {c.get('content', '')[:60]}（{c.get('likes', 0)} 赞）"
        )
    return "\n".join(lines)
