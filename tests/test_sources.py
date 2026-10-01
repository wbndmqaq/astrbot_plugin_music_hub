"""音源 token / 词归一（core.sources）。"""

from astrbot_plugin_music_hub.core.sources import token_to_source, word_to_source


def test_known_tokens():
    assert token_to_source("ncm") == "ncm"
    assert token_to_source("kg") == "kg"
    assert token_to_source("qq") == "qq"


def test_case_and_blank():
    assert token_to_source("NCM") == "ncm"
    assert token_to_source("", default="auto") == "auto"
    assert token_to_source(None, default="") == ""


def test_unknown_token_falls_back():
    assert token_to_source("xxx", default="keep") == "keep"


def test_word_to_source():
    assert word_to_source("网易") == "ncm"
    assert word_to_source("酷狗") == "kg"
    assert word_to_source("QQ") == "qq"
