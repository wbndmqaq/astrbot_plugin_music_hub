"""三平台音质阶梯与统一展示标签。

纯定义模块：只含常量与纯函数，不依赖 HTTP 客户端（避免 api 包的初始化环）。
"""

from __future__ import annotations


def first(*vals) -> str:
    """宽松取首个非空值（上游字段形态不稳定，缺字段层层兜底）。"""
    for v in vals:
        if v:
            return str(v)
    return ""


# ── 网易云（song/url/v1 level）──
NCM_LADDER = [
    "jymaster",
    "dolby",
    "sky",
    "vivid",
    "jyeffect",
    "hires",
    "lossless",
    "exhigh",
    "higher",
    "standard",
]
NCM_LABEL = {
    "auto": "自动适配",
    "jymaster": "超清母带",
    "dolby": "杜比全景声",
    "sky": "沉浸环绕声",
    "vivid": "臻音全景声",
    "jyeffect": "高清环绕声",
    "hires": "Hi-Res",
    "lossless": "无损 FLAC",
    "exhigh": "极高",
    "higher": "较高",
    "standard": "标准",
}
# 无高档位特权时 auto 的起试档（匿名/非会员试母带只会白打 4 次失败请求）
NCM_AUTO_START = "lossless"

# ── 酷狗（song/url quality + 每档专属 hash）──
# 只保留旧版 /song/url 支持的档位：viper/super/high 属新版 /song/url/new，
# 对旧端点是无效 quality，只会白打失败请求再降级
KG_LADDER = ["flac", "320", "128"]
KG_LABEL = {
    "auto": "自动适配",
    "flac": "无损 FLAC",
    "320": "高品 320K",
    "128": "标准 128K",
}

# ── QQ 音乐（SongFileType）──
# 阶梯项为 (展示名, 主档, 兜底档)：master/atmos 匿名必 104003，降到 flac/320/128
QQ_LADDER = [
    ("master", "MASTER", None),
    ("atmos_db", "ATMOS_DB", None),
    ("atmos", "ATMOS_51", "ATMOS_2"),
    ("flac", "FLAC", None),
    ("320", "MP3_320", None),
    ("128", "MP3_128", None),
]
QQ_LABEL = {
    "auto": "自动适配",
    "master": "臻品母带",
    "atmos_db": "杜比全景声",
    "atmos": "全景声",
    "flac": "无损 FLAC",
    "320": "HQ 320K",
    "128": "标准 128K",
    "try": "试听",
}


def ladder_for(source: str, preferred: str = "auto", *, full: bool = False) -> list[str]:
    """从偏好档起向下的候选阶梯（各源返回自己的档位名列表）。

    - ncm：``full=True``（有高档特权）时 auto 从最高档起试，否则从 lossless 起
    - kg / qq：auto 从最高档起试（VIP 档失败自动降级，代价可接受）
    """
    if source == "ncm":
        q = (preferred or "auto").lower()
        if q in ("auto", "adaptive", "best"):
            start = 0 if full else NCM_LADDER.index(NCM_AUTO_START)
            return list(NCM_LADDER[start:])
        idx = NCM_LADDER.index(q) if q in NCM_LADDER else NCM_LADDER.index(NCM_AUTO_START)
        return NCM_LADDER[idx:]
    if source == "kg":
        q = (preferred or "auto").lower()
        if q in ("auto", "adaptive", "best"):
            return list(KG_LADDER)
        idx = KG_LADDER.index(q) if q in KG_LADDER else 0
        return KG_LADDER[idx:]
    if source == "qq":
        q = (preferred or "auto").lower()
        names = [item[0] for item in QQ_LADDER]
        if q in ("auto", "adaptive", "best"):
            return names
        idx = names.index(q) if q in names else 0
        return names[idx:]
    return []


def quality_label(source: str, quality: str) -> str:
    table = {"ncm": NCM_LABEL, "kg": KG_LABEL, "qq": QQ_LABEL}.get(source, {})
    return table.get(quality, quality or "")


def kg_hash_for(song: dict, quality: str) -> str:
    """酷狗：取某音质的专属 hash。"""
    q = (quality or "").lower()
    if q == "flac":
        return first(song.get("hash_flac"), song.get("hash_high"))
    if q == "320":
        return first(song.get("hash_320"), song.get("hash_flac"))
    return first(song.get("hash_128"), song.get("hash"), song.get("FileHash"))


def trial_suffix(play: dict) -> str:
    """取链结果 → 音质标签（试听追加标注）。"""
    label = play.get("qualityLabel") or ""
    if play.get("trial"):
        return f"{label}（试听 60s）" if label else "试听 60s"
    if play.get("unblocked"):
        return f"{label}（解灰）" if label else "解灰"
    return label
