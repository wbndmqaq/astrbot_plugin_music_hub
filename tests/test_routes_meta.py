"""路由表元测试：AstrBot 以 ``__module__+__name__`` 为键绑定 handler，
路由名重复会静默互相覆盖、doc 缺失会在 WebUI 显示「无描述」、run 签名漂移
会破坏包装器的统一调用约定。这里对 ``all_routes()`` 的数据结构整体断言。

install() 的调用机制（handlers/__init__.py）：给每个路由造一个 wrapper 并
setattr 到插件类上、重写 ``__module__``。本文件用一个**普通类**（不是 Star
子类，不触发 ``__init_subclass__`` 注册）+ 假 filter 模块验证 install 的
返回值与挂载结果，不触碰星图。
"""

from __future__ import annotations

import inspect
import re

from astrbot_plugin_music_hub.handlers import all_routes, install


class _FakeFilter:
    """install() 只用到 regex / event_message_type / permission_type 三个工厂与 PermissionType.ADMIN。"""

    class PermissionType:
        ADMIN = "admin"

    @staticmethod
    def regex(pattern, priority=0):
        return lambda fn: fn

    @staticmethod
    def event_message_type(t, priority=0):
        return lambda fn: fn

    @staticmethod
    def permission_type(perm, raise_error=False, priority=0):
        return lambda fn: fn


def test_route_names_unique():
    names = [r.name for r in all_routes()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"路由名重复会以 __module__+__name__ 为键静默互相覆盖：{dupes}"


def test_route_docs_nonempty():
    for r in all_routes():
        assert r.doc and r.doc.strip(), f"{r.name} 缺 doc（WebUI 指令列表会显示「无描述」）"


def test_route_run_is_two_param_coroutine():
    for r in all_routes():
        assert inspect.iscoroutinefunction(r.run), f"{r.name}.run 必须是协程函数"
        params = list(inspect.signature(r.run).parameters.values())
        assert len(params) == 2, f"{r.name}.run 应为 (service, event) 两参，实际 {len(params)} 个"
        kinds = [p.kind for p in params]
        assert all(k is inspect.Parameter.POSITIONAL_OR_KEYWORD for k in kinds), (
            f"{r.name}.run 参数形态异常：{kinds}"
        )


def test_route_patterns_are_precompiled():
    for r in all_routes():
        assert r.pattern is None or isinstance(r.pattern, re.Pattern), (
            f"{r.name} 的 pattern 应预编译为 Pattern（或 None），否则每次触发都重新编译"
        )


def test_admin_routes_are_labelled():
    """admin 路由 doc 统一以「（管理员）」结尾（对照 queue.py 既有写法），非 admin 不占用该标注。"""
    for r in all_routes():
        if r.admin:
            assert r.doc.endswith("（管理员）"), f"{r.name} 是管理员路由，doc 应以「（管理员）」结尾"
        else:
            assert "（管理员）" not in r.doc, f"{r.name} 非管理员路由，doc 不应标（管理员）"


def test_install_returns_route_count_and_mounts_routes():
    routes = all_routes()

    class _Throwaway:  # 普通类：不会进星图（Star.__init_subclass__ 只认 Star 子类）
        service = None

    installed = install(_Throwaway, _FakeFilter(), "tests.test_routes_meta", routes)
    assert installed == len(routes), "install() 返回的已装载数必须与路由表条数一致"
    for r in routes:
        fn = getattr(_Throwaway, r.name)
        assert fn.__name__ == r.name
        assert fn.__doc__ == r.doc, f"{r.name} 挂载后 docstring 应与路由表一致"
        assert fn.__module__ == "tests.test_routes_meta", "wrapper 的 __module__ 应重写为宿主模块"
