"""接口适配约束：网易云固定基础地址 + 酷狗统一 4000 端口 + QQ 走内置依赖。

对应 AUDIT_REPORT 模块 2。这三条是部署契约，改动会让开箱可用的音源直接消失，
因此用测试钉住，而不是只写在 README 里。
"""

import json
from pathlib import Path

import pytest
from astrbot_plugin_music_hub.core.config import Config

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"

NCM_BASE = "http://120.26.120.184:3000"
KG_BASE = "http://127.0.0.1:4000"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text("utf-8"))


# ── 网易云：固定基础地址 ──
def test_ncm_default_base_is_pinned(schema):
    assert schema["ncm"]["items"]["apiBase"]["default"] == NCM_BASE


def test_ncm_base_uses_port_3000(schema):
    assert schema["ncm"]["items"]["apiBase"]["default"].rstrip("/").endswith(":3000")


def test_ncm_hint_documents_the_address(schema):
    hint = schema["ncm"]["items"]["apiBase"].get("hint", "")
    assert "120.26.120.184" in hint, "hint 应给出实际可用的地址，而不只是占位示例"


# ── 酷狗：统一 4000 ──
def test_kg_default_base_uses_port_4000(schema):
    assert schema["kg"]["items"]["apiBase"]["default"] == KG_BASE


def test_kg_hint_documents_port_4000(schema):
    assert "4000" in schema["kg"]["items"]["apiBase"].get("hint", "")


def test_no_schema_hint_recommends_stale_kg_port(schema):
    """回归：曾把酷狗示例写成 3000，与服务端实际默认端口冲突。"""
    for src in ("kg",):
        hint = schema[src]["items"]["apiBase"].get("hint", "")
        assert "127.0.0.1:3000" not in hint, "酷狗端口应统一为 4000"


# ── 空值回落：AstrBot 不会给已存在的键改默认值 ──
def test_empty_ncm_api_base_falls_back_to_default():
    """老配置里 ncm.apiBase 是空串时也必须能用网易云，而不是静默禁用。"""
    cfg = Config({"ncm": {"apiBase": ""}})
    assert cfg.src_api_base("ncm") == NCM_BASE
    assert cfg.src_enabled("ncm") is True


def test_empty_kg_api_base_falls_back_to_default():
    cfg = Config({"kg": {"apiBase": ""}})
    assert cfg.src_api_base("kg") == KG_BASE
    assert cfg.src_enabled("kg") is True


def test_missing_source_node_falls_back_to_default():
    cfg = Config({})
    assert cfg.src_api_base("ncm") == NCM_BASE
    assert cfg.src_api_base("kg") == KG_BASE


def test_explicit_user_override_is_respected():
    """用户显式改成自建地址时必须生效，不能被默认值覆盖。"""
    cfg = Config({"ncm": {"apiBase": "http://192.168.1.9:3000"}})
    assert cfg.src_api_base("ncm") == "http://192.168.1.9:3000"
    cfg2 = Config({"kg": {"apiBase": "http://10.0.0.2:4000"}})
    assert cfg2.src_api_base("kg") == "http://10.0.0.2:4000"


def test_whitespace_only_falls_back_to_default():
    cfg = Config({"ncm": {"apiBase": "   "}})
    assert cfg.src_api_base("ncm") == NCM_BASE


def test_no_protocol_prefix_still_usable():
    cfg = Config({"kg": {"apiBase": "127.0.0.1:4000"}})
    assert cfg.src_api_base("kg") == "http://127.0.0.1:4000"


# ── QQ：内置依赖，不走外部服务 ──
def test_qq_is_local_and_always_enabled():
    cfg = Config({})
    assert cfg.src_api_base("qq") == "local"
    assert cfg.src_enabled("qq") is True


def test_qq_schema_has_no_api_base_node(schema):
    assert "apiBase" not in schema["qq"]["items"], "QQ 走内置库，不应有 apiBase 配置项"


def test_qq_client_does_not_use_http_transport():
    """回归护栏：QQ 客户端不得引入 aiohttp/requests —— 必须走进程内 qqmusic_api。"""
    src = (Path(__file__).resolve().parents[1] / "core" / "api" / "qq.py").read_text("utf-8")
    for banned in ("import aiohttp", "import requests", "import httpx", "get_session("):
        assert banned not in src, f"core/api/qq.py 不应出现 {banned}"


def test_qq_client_imports_builtin_library():
    src = (Path(__file__).resolve().parents[1] / "core" / "api" / "qq.py").read_text("utf-8")
    assert "from qqmusic_api import" in src


def test_qq_declared_in_requirements():
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text("utf-8")
    assert "qqmusic-api-python" in req


# ── 无任何硬编码端口残留 ──
def test_no_hardcoded_ports_in_clients():
    """两个 HTTP 客户端都必须从配置读地址，不得内置端口常量。"""
    api_dir = Path(__file__).resolve().parents[1] / "core" / "api"
    for name in ("ncm.py", "kg.py"):
        src = (api_dir / name).read_text("utf-8")
        for port in (":3000", ":4000"):
            assert port not in src, f"core/api/{name} 不应硬编码 {port}"


def test_enabled_sources_includes_all_three_by_default():
    cfg = Config({})
    assert set(cfg.enabled_sources()) == {"ncm", "kg", "qq"}


# ── 文档/UI 文案与实际默认值不得漂移 ──
def test_webui_placeholder_matches_kg_port():
    app = (Path(__file__).resolve().parents[1] / "webui" / "app.js").read_text("utf-8")
    assert "4000" in app
    assert "kg-apiBase" in app


def test_readme_documents_both_default_addresses():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text("utf-8")
    assert NCM_BASE in readme, "README 应写出网易云默认地址"
    assert "4000" in readme, "README 应写出酷狗端口"
    assert "内置 `qqmusic-api-python`" in readme, "README 应说明 QQ 走内置依赖、无需外部服务"


def test_schema_defaults_match_code_constants(schema):
    """schema default 与 core.DEFAULT_API_BASE 必须一致，否则 WebUI 面板显示与实际行为不符。"""
    from astrbot_plugin_music_hub.core import DEFAULT_API_BASE

    for src, expected in DEFAULT_API_BASE.items():
        assert schema[src]["items"]["apiBase"]["default"] == expected, f"{src} schema 与代码常量不一致"
