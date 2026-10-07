# astrbot_plugin_music_hub

AstrBot 聚合点歌插件：QQ / 酷狗 / 网易云三平台搜歌、点歌、歌词、评论、排行榜，聊天扫码登录，附带 WebUI 管理面板。


## 功能

- **聚合点歌**：`点歌 晴天` 三平台混搜，同名同歌手合并成一行、并列各音源版本；`点歌 ncm:晴天` 指定音源；默认音源可配置（设为具体音源后 `点歌` / `播放` 单源搜索）；取流失败自动换源重试，`换源 音源` 手动换源重放
- **点歌台**：`排队 关键词` 进队列按序播放，`队列` 查看，管理员 `切歌` / `清空队列`
- **歌词**：`歌词`（LRC）/ `逐字歌词`（KRC、QRC），长歌词用 `歌词下页` / `歌词上页` 翻页，带翻译
- **歌曲扩展**：`评论`（`评论下页` 看最新，网易云/QQ）、`专辑评论` / `歌单评论`、`相似`、`相似歌单`（网易云）、`MV搜 关键词`、`高潮` / `版本`（酷狗/QQ）、`AI推荐`（酷狗）
- **发现**：排行榜、新歌、歌手/专辑/歌单、热搜、日推、FM、MV 等浏览类指令，三平台各有特色入口，完整清单见下方指令表
- **账号**：`喜欢`（红心列表）、`红心` / `取消红心` 当前歌曲、`关注列表`、`听歌排行`、`我的歌单`、`云盘`、`已购`（酷狗）、`最近在听`、`历史`
- **订阅推送**：`订阅 日推`、`订阅新歌 歌手名`（自动关注酷狗歌手），每天定时推送到群；`退订 日推` / `退订新歌 歌手名`，`我的订阅` 查看全部（订阅指令均为管理员权限）
- **登录**：`ncm登录 / kg登录 / qq登录` 或 WebUI 扫码，凭证自动保存；QQ 凭证每日保活，网易云每日签到
- **发送**：语音 + 群文件双通道，OneBot base64 直发，失败自动 ffmpeg 压缩重试；OneBot 平台可发原生音乐卡片
- **链接解析**：三平台分享链接和 JSON 音乐卡片自动识别点歌
- **LLM 工具**：注册 `music_hub_play` / `music_hub_lyric` / `music_hub_search` 三个函数工具（同样受总开关/黑白名单/冷却约束），开了函数调用的会话里直接说"来首晴天"就能点歌
- **管理**：黑白名单、点歌冷却、音源限速、调用统计、`音质 <档位>` 设置最高音质、`开启/关闭 点歌|解析|卡片|语音|文件` 临时开关

## 安装

AstrBot WebUI 插件市场搜索 `astrbot_plugin_music_hub`

卡片渲染要 Playwright，不装就自动回退纯文本，不影响点歌：

```bash
pip install playwright
python -m playwright install chromium
# 国内加速：PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ python -m playwright install chromium
# Linux 容器缺库：python -m playwright install-deps chromium
```

## 三音源接入

| 音源 | 方式 | 默认配置 |
|---|---|---|
| QQ 音乐 | 内置 `qqmusic-api-python` 直连官方接口 | 免配置，装完即用；VIP 歌需扫码登录 |
| 网易云 | `api-enhanced` 服务 | 自建 |
| 酷狗 | `KuGouMusicApi` 服务 | 自建 |

### 关于 API 服务地址

QQ 音乐走插件依赖内置库，不需要任何外部服务。网易云与酷狗需要各搭配一个 HTTP API 服务：
- **网易云** —— 自建：[NeteaseCloudMusicApiEnhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)
- **酷狗** —— 自建：[KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)
  注意：酷狗已限制匿名搜索（上游错误码 152），服务启动后请执行 `kg登录` 扫码，再正常使用。
  

- **群公开API** —— 群已配置公益API：[QQ群](https://qm.qq.com/q/8sOZdZTnaw)
  , 群里API公开地装时在配置面板填好A勿高频调用。插件已内置每源 250ms 请求间隔限速（`rateLimitMs`），请勿调低。
> 网易云 APII 以 query 参数传递 Cookie，插件日志已做脱敏处理；自行排查时不要把配置文件外发。

## WebUI

默认 `http://127.0.0.1:17818`（仅本机访问），密码在插件配置 `webui.password`（留空则首次启动随机生成）。
需要局域网其他设备访问时把 `webui.host` 改为 `0.0.0.0`；用域名访问需在 `webui.hostAllowlist` 登记（面板有严格的 Host 白名单校验，未登记的域名会被拒绝）。

功能：多音源搜索（单曲/歌单/专辑/歌手分栏，可试听、可投递到指定群）、扫码登录、调用统计（含失败原因聚合）、黑白名单、全量配置编辑、链接解析、运行日志。

## 架构

### 目录结构

```
astrbot_plugin_music_hub/
├── main.py                  # Star 入口：初始化 MusicService、注册 3 个 LLM 工具
├── core/
│   ├── service.py           # MusicService：所有子模块的装配点与生命周期
│   ├── api/
│   │   ├── base.py          # SourceClient Protocol + missing_methods 自检
│   │   ├── registry.py      # register / create / verify：source id → 客户端工厂
│   │   ├── ncm.py kg.py qq.py   # 三个客户端，文件末尾各register() 一行
│   │   └── http.py          # 共享 aiohttp ClientSession
│   ├── search.py            # SongSearch（搜索编排）+ SearchResult
│   ├── delivery.py          # 投递：_fetch_audio / _send_voice / _send_file
│   ├── media.py             # 音频下载、语音压制、临时文件清理
│   ├── resolve.py           # 分享链接与卡片解析
│   ├── sources.py           # 音源标识与输入解析（token / 词 → 规范音源 id）
│   ├── registry.py          # 会话注册表：scope → unified_msg_origin 映射与持久化
│   ├── catalog.py           # 发现类（榜单/歌单/专辑/歌手）三平台适配
│   ├── remote.py            # 无 event 场景的主动投递（WebUI / 点歌台 / 订阅）
│   ├── quality.py           # 三平台音质阶梯与展示标签（纯定义）
│   ├── matching.py          # 同名同歌手分组、多源交错混排
│   ├── login.py             # LoginFlows（扫码登录会话）
│   ├── queue.py             # RequestDesk（点歌台队列）
│   ├── subs.py              # Subscriptions（订阅推送）
│   ├── scheduler.py         # 每日签到/ 保活 / 订阅推送定时任务
│   ├── ratelimit.py         # RateLimiter 模块级单例
│   ├── platform_caps.py     # 平台能力矩阵 PlatformCaps
│   ├── render.py cards.py formatters.py   # 卡片渲染与纯文本兜底
│   ├── onebot.py color.py help_data.py    # OneBot 直发 / 封面取色 / 帮助卡数据
│   ├── webui/               # WebUI 服务：_server / _auth / _api_*
│   ├── config.py acl.py session.py stats.py history.py logs.py errors.py
│   │   # SOURCES / SOURCE_NAMES 常量在 core/__init__.py
├── handlers/                # 指令路由层，每模块导出 routes()
├── resources/html/          # Jinja2 卡片模板（list / lyric / comment / help ...）
├── webui/                   # 管理面板前端（app.js / style.css / index.html）
└── tests/                # pytest 套件
```


## 指令

| 指令 | 说明 |
|---|---|
| `点歌 关键词` | 三平台聚合搜索，同名同歌手合并出多音源版本 |
| `点歌 ncm:关键词` | 指定音源（`kg:` / `qq:` 同理） |
| `ncm点歌 / kg点歌 / qq点歌` | 音源前缀别名 |
| `听N` / `听N 音源` / `听所有` | 播放列表第 N 首（可指定音源版本）/ 串行连播 |
| `换源 音源` | 把当前歌换音源重新取流 |
| `播放 关键词` | 直接播放首个结果 |
| `歌词 / 逐字歌词 [关键词]` | 查看歌词（先选歌再查看），`歌词下页 / 歌词上页` 翻页 |
| `评论 / 相似 / MV / 高潮 / 版本 [关键词]` | 歌曲扩展信息（高潮仅酷狗、版本酷狗/QQ） |
| `专辑评论 / 歌单评论 / 相似歌单 / AI推荐 [关键词]` | 更多歌曲与歌单扩展 |
| `排行榜 [榜单名]` / `新歌 [地区]` / `歌手 X` / `歌手专辑 X` / `专辑 X` / `歌单 X` | 浏览发现 |
| `热搜` / `来首歌` / `搜索建议 X` / `日推` / `历史日推` / `FM` / `新碟 [地区]` / `歌手榜` | 更多发现 |
| `MV榜 / 电台 / 热门歌手 / banner`（网易云）、`排行推荐 / 编辑精选 / 乐库 / 主题歌单 / 好歌精选`（酷狗）、`歌手MV / 相似歌手 / 猜你喜欢`（QQ） | 音源特色 |
| `歌单推荐 [分类]` / `相关歌单 X` / `精品歌单 [分类]` / `歌单分类` | 歌单发现 |
| `订阅 日推` / `订阅新歌 歌手名` / `退订 日推 / 退订新歌 歌手名` / `我的订阅` | 订阅推送管理（管理员） |
| `排队 关键词` / `队列` / `切歌` / `清空队列` | 点歌台队列（切歌/清空为管理员） |
| `ncm登录 / kg登录 / qq登录` | 扫码登录（管理员） |
| `状态` / `ncm登出` 等 | 登录状态 / 退出（管理员） |
| `帮助` | 本帮助（全员） |
| `设置` / `音质 <档位>` / `统计` / `测试` / `WebUI` | 系统管理（管理员） |

所有指令支持 `#` 前缀（如 `#点歌 晴天`）。

## 配置（节选）

| 配置 | 默认 | 说明 |
|---|---|---|
| `defaultSource` | `auto` | 不带前缀点歌的默认音源；auto = 三平台混搜 |
| `ncm.apiBase` | `http://120.26.120.184:3000` | 网易云 API 服务地址，留空停用网易云音源 |
| `kg.apiBase` | `http://127.0.0.1:4000` | 酷狗 API 服务地址，留空停用酷狗音源 |
| `*.quality` | `auto` | 各音源最高音质，取不到自动降级 |
| `ncm.qualityUnblock` | true | VIP 歌 unblock 解灰兜底 |
| `sendVocal` / `uploadFile` | true | 语音 / 文件双通道 |
| `ffmpegCompress` | true | OneBot 文件通道 >5MB 文件直接发压缩版（并非失败后才重试；需系统 ffmpeg） |
| `compressBitrate` | 128 | 压缩版 mp3 的码率（kbps），与语音压制共用 |
| `rateLimitMs` | 250 | 音源请求最小间隔，0 = 关闭。**改完需重载插件**（限速器是模块级单例，只在启动时按配置初始化） |
| `acl.mode` | `off` | 黑白名单模式（WebUI 可视化管理） |
| `stats.enable` | true | 调用统计开关，关掉后不记录天/音源/动作计数，`统计` 指令与 WebUI 图表无数据 |
| `webui.host` | `127.0.0.1` | 面板监听地址，局域网访问改 `0.0.0.0` |
| `webui.port` | `17818` | 管理面板端口（改完同样需重载插件） |

完整配置在 WebUI「插件配置」页编辑。

## 平台支持

| 能力 | aiocqhttp | QQ官方 | Telegram | Kook/Discord | 微信个人 | 飞书 | 钉钉 |
|---|---|---|---|---|---|---|---|
| 文本/卡片 | 是 | 是 | 是 | 是 | 是 | 是 | 是（图片仅 http 链接） |
| 语音 | 是 | 是 (silk) | 是 | 是 | 否 | 否 | 否 |
| 群文件 | 是 | 是（>10MB 分片） | 是 | 是 | 是 | 否 | 否 |
| 原生音乐卡 | 是 | 否 | 否 | 否 | 否 | 否 | 否 |

钉钉等不支持本地图片/语音的平台，列表卡片自动回退纯文本，歌曲以直链文本投递。
不支持语音的平台（微信个人、钉钉）走群文件通道投递；语音与文件都不支持的（飞书）只发文本提示；文件通道也关闭时同样只发文本提示。


## 致谢

- QQ 音乐接口：[qqmusic-api-python](https://github.com/L-1124/QQMusicApi)（GPLv3）
- 酷狗接口：自部署 [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（
- 网易云接口署 [NeteaseCloudMusicApiEnhanced](https://github.com/neteaudmusicapienhanced/api-enhanced)(MIT)
）   [KuG uNeteaseCloudMusicApiEnhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)



更新日志见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT，见 [LICENSE](LICENSE)。依赖的 qqmusic-api-python 为 GPLv3。

[qqmusic-api-python](https://github.com/L-1124/QQMusicApi)