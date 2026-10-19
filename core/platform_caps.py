"""平台适配能力矩阵：不同聊天平台对媒体/卡片的支持差异统一在这里声明。

delivery / render / resolve 按此决策降级路径，避免每个调用点各自 try/平台名。

| 平台 | 语音 | 文件 | 本地图 | 原生卡 | 被动受限 | 说明 |
|---|---|---|---|---|---|---|
| aiocqhttp  | ✓ | ✓ | ✓ | ✓ | ✗ | OneBot 全能力 |
| qq_official| ✓(silk) | ✓(分片) | ✓ | ✗ | ✓ | 文本媒体需合并省配额 |
| telegram   | ✓ | ✓ | ✓ | ✗ | ✗ | |
| kook       | ✓ | ✓ | ✓ | ✗ | ✗ | |
| discord    | ✓ | ✓ | ✓ | ✗ | ✗ | |
| satori     | ✓ | ◐ | ✓ | ✗ | ✗ | 文件视实现而定 |
| weixin_oc  | ✗ | ✓ | ✓ | ✗ | ✗ | 个人微信无语音段 |
| lark       | ✗ | ✗ | ✓ | ✗ | ✗ | 飞书 Record/File 不支持 |
| dingtalk   | ✗ | ✗ | ✗(仅http图) | ✗ | ✗ | 钉钉图片仅 http 链接 → 卡片降文本 |
| webchat    | ◐ | ◐ | ✓ | ✗ | ✗ | |
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlatformCaps:
    vocal: bool = True  # 语音(Record)出站
    file: bool = True  # 群/好友文件(File)出站
    local_image: bool = True  # 本地图片（渲染卡片）
    native_card: bool = False  # OneBot 原生音乐小程序卡
    passive_limited: bool = False  # 被动回复受限（文本+媒体合并发送省配额）
    notes: dict = field(default_factory=dict)


_PRESETS = {
    "aiocqhttp": PlatformCaps(vocal=True, file=True, local_image=True, native_card=True),
    "qq_official": PlatformCaps(
        vocal=True,
        file=True,
        local_image=True,
        passive_limited=True,
        notes={"direct_ext": {"silk", "wav", "mp3", "flac"}},
    ),
    "telegram": PlatformCaps(),
    "kook": PlatformCaps(),
    "discord": PlatformCaps(),
    "satori": PlatformCaps(file=False),
    "weixin_oc": PlatformCaps(vocal=False),
    "lark": PlatformCaps(vocal=False, file=False),
    "dingtalk": PlatformCaps(vocal=False, file=False, local_image=False),
    "webchat": PlatformCaps(vocal=False, file=False),
}

_DEFAULT = PlatformCaps()


def caps_for_name(name: str) -> PlatformCaps:
    """按平台名取能力预设（WebUI 远程投递等没有 event 对象的场景）。

    匹配是顺序敏感的**子串**匹配（按 _PRESETS 迭代序，先命中先返回）：
    - key 不要用过短的词或通用词（如 "qq"），否则自定义平台名会被误归类；
    - 新增 key 必须是全匹配语义的平台标识（协议适配器的 platform_name 原文）。

    子串匹配之外再做一次去分隔符匹配：自定义显示名/别名（"qqofficial" 拼不出
    qq_official）落默认全能力预设会丢 passive_limited 等省配额策略——把双方
    分隔符都剥掉后再比对一次。"""
    name = str(name or "").lower()
    for key, caps in _PRESETS.items():
        if key in name:
            return caps
    flat = name
    for ch in ("_", "-", " "):
        flat = flat.replace(ch, "")
    if flat:
        # 不设 flat != name 守卫：名字本身不含分隔符时（"qqofficial"）flat == name
        # 会让这段匹配被跳过，docstring 自举的这类别名恰好失效；flat 的比对口径
        # 是「key 去下划线后是否为其子串」，与第一段循环并不重复
        for key, caps in _PRESETS.items():
            if key.replace("_", "") in flat:
                return caps
    return _DEFAULT


def caps_of(event) -> PlatformCaps:
    """按事件平台名取能力预设（含子串匹配，如 napcat → aiocqhttp 预设不适用则用默认）。"""
    try:
        return caps_for_name(str(event.get_platform_name() or ""))
    except Exception:  # noqa: BLE001
        return _DEFAULT
