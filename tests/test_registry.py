"""会话注册表（core.registry）—— umo 变更必须触发落盘（v1.0.2 回归）。"""

import pytest
from astrbot_plugin_music_hub.core.registry import UmoRegistry


@pytest.fixture
def store():
    saved = {}

    async def kv_get(_key, default=None):
        return saved.get("key", default)

    async def kv_put(_key, value):
        saved["key"] = value

    return UmoRegistry(kv_get, kv_put), saved


async def test_first_note_triggers_save(store):
    reg, _ = store
    await reg.load()
    assert reg.note("g1", "aiocqhttp:GroupMessage:111") is True


async def test_same_umo_is_quiet(store):
    reg, _ = store
    await reg.load()
    reg.note("g1", "aiocqhttp:GroupMessage:111")
    assert reg.note("g1", "aiocqhttp:GroupMessage:111") is False  # 仅活跃时间戳刷新不落盘


async def test_changed_umo_triggers_save(store):
    """群迁移 / 平台换号后 umo 变化必须通知落盘，否则重启后推送发到失效会话。"""
    reg, saved = store
    await reg.load()
    reg.note("g1", "aiocqhttp:GroupMessage:111")
    await reg.save()
    assert reg.note("g1", "telegram:GroupMessage:222") is True
    await reg.save()
    assert reg.umo_of("g1") == "telegram:GroupMessage:222"
    # 重启恢复链路
    reg2, _ = store
    await reg2.load()
    assert reg2.umo_of("g1") == "telegram:GroupMessage:222"


async def test_empty_umo_ignored(store):
    reg, _ = store
    await reg.load()
    assert reg.note("g1", "") is False
