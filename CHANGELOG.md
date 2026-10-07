# Music Hub 聚合点歌

## v1.0.0 (2026-10-07)

QQ / 酷狗 / 网易云三平台聚合点歌的首个发布版本。

- 点歌：三平台混搜、同名同歌手合并多音源版本、失败自动换源；歌词（含 KRC/QRC 逐字）、评论、排行榜与歌单等发现类指令、点歌台队列、订阅推送、聊天或 WebUI 扫码登录、LLM 函数工具点歌
- WebUI 管理面板（默认 127.0.0.1:17818）：多音源搜索试听、投递到群、调用统计、黑白名单、全量配置、运行日志
- 音源接入：QQ 音乐内置 qqmusic-api-python，开箱即用；网易云 / 酷狗各需一个 HTTP API 服务，默认地址已预填，见 README「三音源接入」

### 依赖与要求

- 需要 AstrBot ≥ 4.28；qq_official 平台的文本媒体合并与 >10MB 分片上传需 ≥ 4.27.3
- Python 依赖：aiohttp / jinja2 / Pillow / qqmusic-api-python（安装时自动装齐）；卡片渲染需要 Playwright Chromium（可选，未装自动回退纯文本）
