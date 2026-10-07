"""core.acl 黑白名单的直接覆盖（P2-13）：此前全仓只有 AST 源码检查、零行为测试。

acl 是 README 宣传的安全特性（名单匹配、平台前缀候选、白名单交集），
这里按 acl.py 的实际实现逐条钉住：off 放行、命中/未命中、前缀与裸 ID
两种形态、条目 strip 容错、空 ID 放行。

大小写说明：acl.py 对名单条目只做 strip，不做大小写归一（平台前缀部分
区分大小写），下面的用例按现状钉住；若日后统一归一需同步调整。
"""

import pytest
from astrbot_plugin_music_hub.core.acl import Acl
from astrbot_plugin_music_hub.core.config import Config
from astrbot_plugin_music_hub.core.errors import AclDeniedError


def make_acl(mode: str, *, blacklist=None, whitelist=None) -> Acl:
    node: dict = {"mode": mode}
    if blacklist is not None:
        node["blacklist"] = blacklist
    if whitelist is not None:
        node["whitelist"] = whitelist
    return Acl(Config({"acl": node}))


def expect_denied(acl: Acl, **kw) -> None:
    with pytest.raises(AclDeniedError):
        acl.check(**kw)


# ──────────── off：完全不限制 ────────────


def test_off_mode_allows_listed_ids():
    acl = make_acl("off", blacklist=["123456"], whitelist=["999"])
    acl.check(sender_id="123456", group_id="777", platform="aiocqhttp")  # 名单在但 mode=off，放行


# ──────────── blacklist：命中拒、未命中放 ────────────


def test_blacklist_blocks_listed_sender():
    acl = make_acl("blacklist", blacklist=["123456"])
    expect_denied(acl, sender_id="123456", group_id="777", platform="aiocqhttp")


def test_blacklist_blocks_listed_group():
    """群号命中即整群拒绝，与发言者是谁无关。"""
    acl = make_acl("blacklist", blacklist=["777"])
    expect_denied(acl, sender_id="123456", group_id="777", platform="aiocqhttp")


def test_blacklist_miss_allows():
    acl = make_acl("blacklist", blacklist=["999"])
    acl.check(sender_id="123456", group_id="777", platform="aiocqhttp")


def test_blacklist_empty_blocks_nobody():
    acl = make_acl("blacklist", blacklist=[])
    acl.check(sender_id="123456", group_id="777", platform="aiocqhttp")


# ──────────── whitelist：命中放、未命中拒 ────────────


def test_whitelist_allows_listed_sender():
    acl = make_acl("whitelist", whitelist=["123456"])
    acl.check(sender_id="123456", group_id="777", platform="aiocqhttp")


def test_whitelist_allows_listed_group():
    """白名单是「用户或群任一命中即放行」：只列群号时群内任何人可用。"""
    acl = make_acl("whitelist", whitelist=["777"])
    acl.check(sender_id="111", group_id="777", platform="aiocqhttp")


def test_whitelist_miss_denies():
    acl = make_acl("whitelist", whitelist=["123456"])
    expect_denied(acl, sender_id="111", group_id="777", platform="aiocqhttp")


def test_whitelist_empty_denies_everyone():
    acl = make_acl("whitelist", whitelist=[])
    expect_denied(acl, sender_id="123456", group_id="777", platform="aiocqhttp")


# ──────────── 条目形态：平台前缀 vs 裸 ID ────────────


def test_blacklist_prefix_form_matches_own_platform_only():
    """``aiocqhttp:123456`` 只封该平台的此用户，其他平台同名 ID 不受影响。"""
    acl = make_acl("blacklist", blacklist=["aiocqhttp:123456"])
    expect_denied(acl, sender_id="123456", group_id="", platform="aiocqhttp")
    acl.check(sender_id="123456", group_id="", platform="telegram")  # 其他平台放行


def test_blacklist_bare_id_form_matches_any_platform():
    acl = make_acl("blacklist", blacklist=["123456"])
    expect_denied(acl, sender_id="123456", group_id="", platform="telegram")
    expect_denied(acl, sender_id="123456", group_id="", platform="aiocqhttp")


def test_prefix_group_entry_blocks_group():
    acl = make_acl("whitelist", whitelist=["aiocqhttp:777"])
    acl.check(sender_id="111", group_id="777", platform="aiocqhttp")  # 群前缀命中 → 放行
    expect_denied(acl, sender_id="111", group_id="888", platform="aiocqhttp")


# ──────────── 容错与边界 ────────────


def test_entry_whitespace_is_stripped():
    """名单条目只 strip 不去大小写：前后空格容错按实现钉住。"""
    acl = make_acl("blacklist", blacklist=["  123456  "])
    expect_denied(acl, sender_id="123456", group_id="", platform="aiocqhttp")


def test_prefix_is_case_sensitive():
    """平台前缀部分区分大小写（实现未做归一）：按现状钉住，防止无声漂移。"""
    acl = make_acl("blacklist", blacklist=["Aiocqhttp:123456"])
    acl.check(sender_id="123456", group_id="", platform="aiocqhttp")  # 大小写不同 → 不命中


def test_empty_ids_pass_even_in_whitelist_mode():
    """空 ID 一律放行（异常消息场景），白名单模式也不例外。"""
    acl = make_acl("whitelist", whitelist=["123456"])
    acl.check(sender_id="", group_id="", platform="aiocqhttp")


def test_invalid_mode_falls_back_to_off():
    """mode 脏值（手改配置）回落 off：拒绝要显式配置，不能靠解析意外。"""
    acl = make_acl("deny_all")
    acl.check(sender_id="123456", group_id="777", platform="aiocqhttp")


def test_acl_node_not_dict_degrades_to_off():
    acl = Acl(Config({"acl": "oops"}))
    acl.check(sender_id="123456", group_id="777", platform="aiocqhttp")
