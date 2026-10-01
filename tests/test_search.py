"""聚合搜索的源收集（core.search）—— 失败音源必须记统计（v1.0.2 回归）。"""

from astrbot_plugin_music_hub.core.errors import NotEnabledError
from astrbot_plugin_music_hub.core.search import SearchManager


class FakeStats:
    def __init__(self):
        self.calls = []

    def record(self, source, action, ok=True, detail=""):
        self.calls.append((source, action, ok))


class FakeService:
    def __init__(self):
        self.stats = FakeStats()


async def test_gather_records_every_source():
    sm = SearchManager.__new__(SearchManager)
    svc = FakeService()
    sm._service = svc

    async def ok():
        return [{"name": "晴天"}]

    async def boom():
        raise RuntimeError("boom")

    async def disabled():
        raise NotEnabledError("未配置", source="kg")

    buckets = await sm._gather_sources(["ncm", "kg", "qq"], [ok(), boom(), disabled()], "晴天", "search")
    assert buckets[0] == [{"name": "晴天"}]
    assert buckets[1] == [] and buckets[2] == []  # 失败源置空桶，不拖垮整体
    marks = {s: ok for s, _, ok in svc.stats.calls}
    assert marks == {"ncm": True, "kg": False, "qq": False}  # 失败也留痕
