"""代码质量护栏：_src_of 收敛为单一实现、死代码不回流、阻塞 I/O 不回流。

对应 AUDIT_REPORT 模块 3 / 1.20 / 1.21。这些不改变功能，但防止后续提交
把已经修掉的缺陷带回来。
"""

import ast
import inspect
from pathlib import Path

import pytest

HANDLERS = Path(__file__).resolve().parents[1] / "handlers"
CORE = Path(__file__).resolve().parents[1] / "core"


def _py_files(root: Path):
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


# ── 3.1 _src_of 必须只有一处实现 ──
def test_src_of_has_single_implementation():
    """回归：_src_of 曾有 5 份拷贝且默认值不一致（auto / '' / 传入），
    新增调用点容易误用导致音源解析静默失败。"""
    sites = []
    for f in _py_files(HANDLERS):
        tree = ast.parse(f.read_text("utf-8"))
        for node in tree.body:  # 只看模块级定义
            if isinstance(node, ast.FunctionDef) and node.name == "_src_of":
                sites.append(f.name)
    assert not sites, f"_src_of 仍有 {len(sites)} 份重复实现：{sites}；应统一到 core.sources"


def test_no_module_level_function_wraps_a_single_core_call():
    """单行包装函数（def f(): return g(x)）属过度抽象，应直接调用 g。"""
    offenders = []
    for f in _py_files(HANDLERS):
        tree = ast.parse(f.read_text("utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("run_"):
                continue
            body = [n for n in node.body if not isinstance(n, ast.Expr)]
            if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Call):
                if not node.decorator_list and not node.args.args and not node.args.kwonlyargs:
                    offenders.append(f"{f.name}:{node.lineno} {node.name}")
    assert not offenders, "这些单行包装函数应内联：\n  " + "\n  ".join(offenders)


# ── 3.2 死代码不得回流 ──
DEAD_SYMBOLS = [
    ("core", "media.py", "_write_bytes"),
    ("core", "media.py", "reset_temp_dir_cache"),
    ("core", "resolve.py", "_EXTRACTORS"),
    ("core", "acl.py", "is_denied"),
    ("core", "cards.py", "build_help_card_data"),
]


@pytest.mark.parametrize("pkg,fname,symbol", DEAD_SYMBOLS, ids=[s for _, _, s in DEAD_SYMBOLS])
def test_dead_symbols_stay_deleted(pkg, fname, symbol):
    """这些符号全仓无读取点；一旦被重新引入说明清理回滚了。"""
    path = Path(__file__).resolve().parents[1] / pkg / fname
    assert symbol not in path.read_text("utf-8"), f"{fname} 中的死代码 {symbol} 已被重新引入"


def test_platform_caps_has_no_standalone_name_helper():
    """platform_name() 曾是死函数（caps_of 内联了同样的 try/except）。"""
    src = (Path(__file__).resolve().parents[1] / "core" / "platform_caps.py").read_text("utf-8")
    assert "def platform_name" not in src


def test_no_module_level_private_import_of_core_cards():
    """core.cards 无反向依赖，函数内重复 import 属冗余防御。"""
    offenders = []
    for f in _py_files(HANDLERS):
        tree = ast.parse(f.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.ImportFrom)
                        and sub.module
                        and sub.module.endswith("cards")
                        and sub.level > 0
                    ):
                        offenders.append(f"{f.name}:{sub.lineno}")
    assert not offenders, "core.cards 应在模块顶层导入：\n  " + "\n  ".join(offenders)


# ── 3.3 事件循环内不得有同步文件 I/O ──
def test_no_sync_file_write_in_handlers():
    """回归：扫码保存二维码曾用同步 open().write() 阻塞事件循环。"""
    offenders = []
    for f in _py_files(HANDLERS):
        tree = ast.parse(f.read_text("utf-8"))
        for node in ast.walk(tree):
            # 找 open(path, "w"/"wb") 形式的直接调用
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else None
                mode_val = getattr(mode, "value", None)
                if isinstance(mode_val, str) and ("w" in mode_val or "a" in mode_val):
                    offenders.append(f"{f.name}:{node.lineno} open(..., {mode_val!r})")
    assert not offenders, "写文件应走 asyncio.to_thread：\n  " + "\n  ".join(offenders)


def test_socket_usage_is_thread_offloaded():
    """回归：局域网 IP 探测曾直接在协程里建 socket，且 close() 不在 finally。"""
    src = (HANDLERS / "system.py").read_text("utf-8")
    assert "import socket" in src
    tree = ast.parse(src)
    # socket 操作必须在普通 def（不是 async def）里，且调用点经 to_thread
    sync_fns = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and "socket" in ast.dump(n)]
    assert sync_fns, "socket 探测应在普通函数中实现"
    assert "to_thread(_detect_lan_ip)" in src, "调用点必须经 asyncio.to_thread"


def test_socket_is_closed_in_finally():
    src = (HANDLERS / "system.py").read_text("utf-8")
    fn = src[src.index("def _detect_lan_ip") : src.index("def _detect_lan_ip") + 700]
    assert "finally" in fn, "socket.close() 必须在 finally 中，否则异常时泄漏句柄"
    assert "s.close()" in fn


# ── 3.4 错误处理不得静默吞掉可观测的失败 ──
def test_share_handler_logs_on_failure():
    """回归：分享链接解析失败曾完全静默（except Exception: return），核心功能故障不可观测。"""
    src = (HANDLERS / "share.py").read_text("utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ExceptHandler):
            body = node.body
            # 允许 re-raise / 记录日志；只允许空 pass + 有注释说明
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                pytest.fail(f"share.py:{node.lineno} 存在空的 except pass，失败不可观测")


def test_acl_rejects_are_not_silent():
    src = (CORE / "acl.py").read_text("utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ExceptHandler) and len(node.body) == 1:
            b = node.body[0]
            if isinstance(b, ast.Pass):
                pytest.fail(f"acl.py:{node.lineno} 存在空的 except pass")


# ── 3.5 统计 action 白名单必须覆盖分类型搜索 ──
def test_search_actions_are_declared_in_stats():
    """回归：分类型搜索用 f'search_{type_}' 记统计，但动作名不在白名单 → 全被归为 other。"""
    from astrbot_plugin_music_hub.core.search import MULTI_TYPE_PARAM
    from astrbot_plugin_music_hub.core.stats import ACTION_NAMES, ACTIONS

    for kind in MULTI_TYPE_PARAM:
        action = f"search_{kind}"
        assert action in ACTIONS, f"{action} 未在 stats.ACTIONS 声明，统计会落到 other"
        assert action in ACTION_NAMES, f"{action} 缺少中文展示名"


def test_stats_chart_iterates_sources_constant():
    """回归：图表曾硬编码三个音源 key，新增平台只更新一半。"""
    from astrbot_plugin_music_hub.core import SOURCES
    from astrbot_plugin_music_hub.core import stats as stats_mod

    src = inspect.getsource(stats_mod)
    # 允许出现字面量，但必须同时存在遍历 SOURCES 的写法
    assert "for src in SOURCES" in src, "统计应遍历 SOURCES 而非硬编码音源列表"
    # 三个内置平台必须齐备（这是下限，不是上限——断言相等会挡住新增平台）
    assert {"ncm", "kg", "qq"} <= set(SOURCES)


def test_no_hardcoded_source_branch_in_search_core():
    """回归：core/search.py 曾按字面量 if source == "qq" 分派搜索类型，
    新增平台会静默落到 else 拿到 param=None（永远返回空列表）。"""
    src = (CORE / "search.py").read_text("utf-8")
    assert 'source == "qq"' not in src, "搜索类型分派应数据驱动（见 MULTI_TYPE_PARAM），不要按字面量分支"
