"""复现用例：路由包装器的开关让路语义与候选项展开的跨模块导入。

对应 AUDIT_REPORT 1.1（导入不存在的符号）与 1.2（总开关关闭时反而吞事件）。
"""

import pytest
from astrbot_plugin_music_hub.handlers import base as base_mod
from astrbot_plugin_music_hub.handlers.base import Route


class FakeService:
    def __init__(self, enable=True):
        self.config = type("C", (), {"enable": enable})()
        self.ran = []
        self.replies = []
        self.logged = []

    def note_umo(self, event):
        pass

    def check_acl(self, event):
        pass

    def log_warn(self, msg):
        self.logged.append(msg)

    async def reply(self, event, text):
        self.replies.append(text)


class FakeEvent:
    def __init__(self):
        self.stopped = 0

    def stop_event(self):
        self.stopped += 1


def _install_one(route: Route):
    """把单条 Route 装到一个临时类上，返回 (cls, route_name)。"""

    class _Cls:
        service = None

    base_mod.install(_Cls, None, "tests.dummy", [route])
    return _Cls, route.name


async def _run(route: Route, service, event):
    cls, name = _install_one(route)
    inst = cls()
    inst.service = service
    await getattr(inst, name)(event)
    return event


# ── 1.2 总开关关闭时必须让路（不吞事件） ──
async def test_master_switch_off_yields_event_untouched():
    async def run(service, event):  # pragma: no cover - 不应被调用
        raise AssertionError("总开关关闭时不应执行路由")

    route = Route(pattern=None, name="dummy", doc="d", run=run, gated=True)
    event = await _run(route, FakeService(enable=False), FakeEvent())
    assert event.stopped == 0, "总开关关闭时调用 stop_event 会阻断其他插件"


async def test_master_switch_on_still_stops_event():
    async def run(service, event):
        service.ran.append(1)
        return True

    route = Route(pattern=None, name="dummy", doc="d", run=run, gated=True)
    svc = FakeService(enable=True)
    event = await _run(route, svc, FakeEvent())
    assert svc.ran == [1]
    assert event.stopped == 1, "正常处理后仍应停止传播"


async def test_route_returning_false_yields_event():
    """run 返回 False 表示让路，此时也不应 stop_event。"""

    async def run(service, event):
        return False

    route = Route(pattern=None, name="dummy", doc="d", run=run, gated=False)
    event = await _run(route, FakeService(), FakeEvent())
    assert event.stopped == 0


async def test_route_exception_is_reported_not_raised():
    async def run(service, event):
        raise RuntimeError("boom")

    route = Route(pattern=None, name="dummy", doc="d", run=run)
    svc = FakeService()
    await _run(route, svc, FakeEvent())
    assert any("boom" in r for r in svc.replies), "异常应转成用户可见回复"
    assert svc.logged, "异常应留日志"


# ── 1.1 候选项展开的跨模块导入必须可达 ──
def test_expand_candidate_is_public_in_defining_module():
    """play.py / actions.py 依赖这两个跨模块 helper，必须是 explore_common 的公开名。"""
    from astrbot_plugin_music_hub.handlers import explore_common

    assert hasattr(explore_common, "expand_candidate")
    assert hasattr(explore_common, "playlists_as_items")
    for legacy in ("_expand_candidate", "_playlists_as_items"):
        assert not hasattr(explore_common, legacy), f"{legacy} 应已提升为公开名"


def test_callers_import_from_defining_module():
    """回归：play.py / actions.py 曾从 handlers.explore 取不存在的符号。"""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "handlers"
    for fname, needed in (("play.py", "expand_candidate"), ("actions.py", "playlists_as_items")):
        tree = ast.parse((root / fname).read_text("utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(a.asname or a.name for a in node.names)
        assert needed in imported, f"{fname} 未导入 {needed}"
        # 不允许再从 explore 汇总模块取（它并不 re-export explore_common 的 helper）
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "explore":
                names = {a.asname or a.name for a in node.names}
                assert needed not in names, f"{fname} 不应从 .explore 导入 {needed}"


@pytest.mark.parametrize("name", ["expand_candidate", "playlists_as_items"])
def test_helpers_have_expected_shape(name):
    import inspect

    from astrbot_plugin_music_hub.handlers import explore_common

    fn = getattr(explore_common, name)
    assert callable(fn)
    assert not inspect.iscoroutinefunction(fn) or name == "expand_candidate"
