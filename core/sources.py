"""音源标识与输入解析（token / 词 → 规范音源 id）的唯一定义。"""

from __future__ import annotations

from . import SOURCE_KG, SOURCE_NCM, SOURCE_QQ

# 指令前缀 / 听N 后缀里出现的音源写法
_TOKEN_MAP = {
    "ncm": SOURCE_NCM,
    "wy": SOURCE_NCM,
    "wangyiyun": SOURCE_NCM,
    "网": SOURCE_NCM,
    "网易云": SOURCE_NCM,
    "kg": SOURCE_KG,
    "kugou": SOURCE_KG,
    "狗": SOURCE_KG,
    "酷狗": SOURCE_KG,
    "qq": SOURCE_QQ,
    "qqm": SOURCE_QQ,
    "qqyy": SOURCE_QQ,
    "q": SOURCE_QQ,
    "qq音乐": SOURCE_QQ,
}

# 换源/音源 参数：用户可能写全称，按包含关系匹配
_WORDS = (
    ("网易云", SOURCE_NCM),
    ("ncm", SOURCE_NCM),
    ("wy", SOURCE_NCM),
    ("酷狗", SOURCE_KG),
    ("kugou", SOURCE_KG),
    ("kg", SOURCE_KG),
    ("qq音乐", SOURCE_QQ),
    ("qq", SOURCE_QQ),
    ("qqm", SOURCE_QQ),
)


def token_to_source(token: str | None, default: str = "auto") -> str:
    """指令里的音源 token（ncm/kg/qq/网/狗…）→ 规范音源 id；认不出给 default。"""
    return _TOKEN_MAP.get((token or "").strip().lower(), default)


def word_to_source(word: str | None, default: str = "") -> str:
    """自然语言里的音源词（换源 网易云 / 换源 kg）→ 规范音源 id。

    双向包含：用户写「网易」是表项「网易云」的子串，写「网易云音乐」又包含它。
    空词直接回默认——空串是任何串的子串，不拦会误命中首个表项。
    """
    w = (word or "").strip().lower()
    if not w:
        return default
    for key, src in _WORDS:
        if key in w or w in key:
            return src
    return default
