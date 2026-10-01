"""状态卡脱敏（core.cards）—— 单段域名不能丢端口（v1.0.2 回归）。"""

from astrbot_plugin_music_hub.core.cards import _mask_base


def test_multi_label_host_keeps_tail_and_port():
    # 设计即「各只露尾段」：域名露 TLD、IPv4 全遮，端口保留
    assert _mask_base("https://music.163.com/song?id=1") == "***.com"
    assert _mask_base("http://api.example.com:3000/x") == "***.com:3000"


def test_single_label_keeps_port():
    assert _mask_base("http://localhost:8080/y") == "***:8080"
    assert _mask_base("http://nas:4000/search") == "***:4000"


def test_ip_is_fully_masked():
    assert _mask_base("http://192.168.1.10:3000/z") == "***.***.***.***:3000"


def test_invalid_url_is_neutralized():
    assert _mask_base("") == "未配置"
    assert _mask_base("not a url") == "***"
