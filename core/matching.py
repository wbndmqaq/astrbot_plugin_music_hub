"""多音源聚合匹配：同名同歌手分组、版本挑选、换源最佳匹配、交错混排。

音源前缀解析（``ncm:晴天``）也在这里 —— 它本质上是对"用户输入里的音源意图"
做归一化，与分组/换源共用同一套音源常量。
"""

from __future__ import annotations

import re

from . import SOURCE_NAMES, SOURCES

# ``ncm:晴天`` / ``kg：晴天`` → (音源, 关键词)；大小写不敏感，前缀别名见 _PREFIX_TOKENS
_SOURCE_PREFIX_RE = re.compile(r"^(ncm|wy|wangyiyun|kg|kugou|qq|qqm|qqyy)\s*[:：]\s*(.+)$", re.IGNORECASE)

_PREFIX_TOKENS = {
    "ncm": "ncm",
    "wy": "ncm",
    "wangyiyun": "ncm",
    "kg": "kg",
    "kugou": "kg",
    "qq": "qq",
    "qqm": "qq",
    "qqyy": "qq",
}


def parse_source_hint(keyword: str) -> tuple[str, str]:
    """``ncm:晴天`` → ("ncm", "晴天")；无前缀 → ("auto", 原关键词)。"""
    m = _SOURCE_PREFIX_RE.match((keyword or "").strip())
    if not m:
        return "auto", (keyword or "").strip()
    src = _PREFIX_TOKENS.get(m.group(1).lower(), "auto")
    return src, m.group(2).strip()


def _norm_key(name: str, artist: str) -> str:
    """分组键：歌名+歌手 小写、去空白与常见后缀噪声。"""
    n = re.sub(r"[\s()（）\[\]【】·]", "", str(name or "")).lower()
    a = re.sub(r"[\s/、,，&]", "", str(artist or "")).lower()
    return f"{n}|{a}"


def group_versions(songs: list[dict], preferred_first: str = "auto") -> list[dict]:
    """把聚合歌曲按 (歌名+歌手) 分组，每组合并出多音源版本列表。

    preferred_first：默认音源（auto 时 ncm>kg>qq），决定组内版本顺序与 primary。
    """
    order = ["ncm", "kg", "qq"]
    if preferred_first in SOURCES:
        order = [preferred_first, *[s for s in order if s != preferred_first]]
    groups: dict[str, dict] = {}
    out: list[dict] = []
    for song in songs:
        key = _norm_key(song.get("name"), song.get("artist"))
        g = groups.get(key)
        if g is None:
            g = {
                "name": song.get("name", ""),
                "artist": song.get("artist", ""),
                "album": song.get("album", ""),
                "cover": song.get("cover", ""),
                "duration": song.get("duration", ""),
                "dtMs": song.get("dtMs", 0),
                "versions": [],
                "vsrcs": set(),
            }
            groups[key] = g
            out.append(g)
        if song.get("source") not in g["vsrcs"]:
            g["vsrcs"].add(song.get("source"))
            g["versions"].append(song)
            if song.get("cover") and not g["cover"]:
                g["cover"] = song.get("cover")
    for g in out:
        g["versions"].sort(key=lambda s: order.index(s["source"]) if s.get("source") in order else 9)
        g["primary"] = g["versions"][0]
        g["vsrcs"] = [s["source"] for s in g["versions"]]
        g["anyFree"] = any(not s.get("pay") for s in g["versions"])
    for i, g in enumerate(out):
        g["index"] = i + 1
    return out


def pick_version(group: dict, source: str = "") -> dict | None:
    """从分组里挑版本：指定音源精确取；为空取 primary。"""
    if not source:
        return group.get("primary")
    for v in group.get("versions", []):
        if v.get("source") == source:
            return v
    return None


def version_names(group: dict) -> str:
    return " / ".join(SOURCE_NAMES.get(s, s) for s in group.get("vsrcs", []))


def best_match(cands: list[dict], name: str, artist: str) -> dict:
    """换源时挑最佳匹配：歌名/歌手相似度最高者优先。"""
    n = re.sub(r"[\s()（）\[\]【】]", "", (name or "")).lower()
    a = re.sub(r"[\s/、,，&]", "", (artist or "")).lower()

    def score(s: dict) -> int:
        sn = re.sub(r"[\s()（）\[\]【】]", "", (s.get("name") or "")).lower()
        sa = re.sub(r"[\s/、,，&]", "", (s.get("artist") or "")).lower()
        return (2 if sn == n else (1 if n and n in sn else 0)) * 10 + (2 if a and (a in sa or sa in a) else 0)

    return max(cands, key=score)


def interleave(buckets: list[list[dict]]) -> list[dict]:
    """多源结果轮流交错（保持各源内部顺序），并重编 index。"""
    out = []
    max_len = max((len(b) for b in buckets), default=0)
    for i in range(max_len):
        for b in buckets:
            if i < len(b):
                out.append(b[i])
    for i, s in enumerate(out):
        s["index"] = i + 1
    return out
