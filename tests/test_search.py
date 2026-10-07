"""聚合搜索的源收集（core.search）—— 失败音源必须记统计（v1.0.2 回归）。"""

from astrbot_plugin_music_hub.core.errors import ApiError, NotEnabledError
from astrbot_plugin_music_hub.core.search import SongSearch


class FakeStats:
    def __init__(self):
        self.calls = []

    def record(self, source, action, ok=True, detail=""):
        self.calls.append((source, action, ok))


class FakeConfig:
    max_list = 10
    default_source = "auto"

    def src_enabled(self, source):
        return True


def _search(stats):
    return SongSearch(FakeConfig(), {}, stats, lambda: ["ncm", "kg", "qq"])


async def test_gather_records_every_source():
    stats = FakeStats()
    sm = _search(stats)

    async def ok():
        return [{"name": "晴天"}]

    async def boom():
        raise RuntimeError("boom")

    async def disabled():
        raise NotEnabledError("未配置", source="kg")

    buckets, notices = await sm._gather_sources(
        ["ncm", "kg", "qq"], [ok(), boom(), disabled()], "晴天", "search"
    )
    assert buckets[0] == [{"name": "晴天"}]
    assert buckets[1] == [] and buckets[2] == []  # 失败源置空桶，不拖垮整体
    marks = {s: ok for s, _, ok in stats.calls}
    assert marks == {"ncm": True, "kg": False, "qq": False}  # 失败也留痕
    assert notices == []


async def test_gather_collects_login_notice_per_call():
    """需登录提示必须随本次调用返回，不挂在实例上——否则并发搜索会互相取走对方的提示。"""
    stats = FakeStats()
    sm = _search(stats)

    async def need_login():
        raise ApiError("未登录或登录已失效", source="ncm")

    async def fine():
        return [{"name": "ok"}]

    _, notices_a = await sm._gather_sources(["ncm"], [need_login()], "a", "search")
    _, notices_b = await sm._gather_sources(["ncm"], [fine()], "b", "search")

    assert notices_a and "网易云" in notices_a[0]
    assert notices_b == [], "第二次搜索不应拿到上一次的提示"


async def test_single_source_search_failure_is_reraised():
    """单源模式失败要抛出并记 ok=False，不能像聚合那样吞掉。"""
    stats = FakeStats()
    sm = _search(stats)

    async def boom():
        raise ApiError("接口挂了", source="ncm")

    try:
        await sm._call("ncm", "search", boom(), detail="x")
    except ApiError:
        pass
    else:
        raise AssertionError("单源失败应向上抛")
    assert stats.calls == [("ncm", "search", False)]
