# astrbot_plugin_music_hub

AstrBot 聚合点歌插件：QQ / 酷狗 / 网易云三平台搜歌、点歌、歌词、评论、排行榜，聊天扫码登录，附带 WebUI 管理面板。

![help](https://img.shields.io/badge/AstrBot-%3E%3D4.17-blue)

## 功能

- **聚合点歌**：`点歌 晴天` 三平台混搜，同名同歌手合并成一行、并列各音源版本；`点歌 ncm:晴天` 指定音源；默认音源可配置（设为具体音源后 `点歌` / `播放` 单源搜索）；取流失败自动换源重试，`换源 音源` 手动换源重放
- **点歌台**：`排队 关键词` 进队列按序播放，`队列` 查看，管理员 `切歌` / `清空队列`
- **歌词**：`歌词`（LRC）/ `逐字歌词`（KRC、QRC），长歌词用 `歌词下页` / `歌词上页` 翻页，带翻译
- **歌曲扩展**：`评论`（`评论下页` 看最新，网易云/QQ）、`专辑评论` / `歌单评论`、`相似`、`相似歌单`（网易云）、`MV搜 关键词`、`高潮` / `版本`（酷狗/QQ）、`AI推荐`（酷狗）
- **发现**：`排行榜` / `排行推荐`、`新歌`、`歌手` / `歌手专辑` / `歌手MV` / `相似歌手`（QQ）、`专辑`、`歌单` / `相关歌单` / `精品歌单`、`歌单推荐`、`主题歌单` / `好歌精选` / `编辑精选` / `乐库`（酷狗）、`热搜`、`搜索建议`、`来首歌` / `猜你喜欢`、`日推` / `历史日推`、`FM`、`MV榜` / `电台` / `热门歌手`、`新碟 [地区]`、`歌手榜`、`歌单分类`
- **账号**：`喜欢`（红心列表）、`红心` / `取消红心` 当前歌曲、`关注列表`、`听歌排行`、`我的歌单`、`云盘`、`已购`（酷狗）、`最近在听`、`历史`
- **订阅推送**：`订阅 日推`、`订阅新歌 歌手名`（自动关注酷狗歌手），每天定时推送到群；`退订 日推` / `退订新歌 歌手名`，`我的订阅` 查看全部
- **登录**：`ncm登录 / kg登录 / qq登录` 或 WebUI 扫码，凭证自动保存；QQ 凭证每日保活，网易云每日签到
- **发送**：语音 + 群文件双通道，OneBot base64 直发，失败自动 ffmpeg 压缩重试；OneBot 平台可发原生音乐卡片
- **链接解析**：三平台分享链接和 JSON 音乐卡片自动识别点歌
- **LLM 工具**：注册 `music_hub_play` 等函数工具（同样受总开关/黑白名单/冷却约束），开了函数调用的会话里直接说"来首晴天"就能点歌
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

| 音源 | 方式 | 配置 |
|---|---|---|
| QQ 音乐 | 内置 qqmusic-api-python 直连 | 开箱即用，VIP 歌需登录 |
| 网易云 | 自建 [api-enhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced) 服务 | 插件配置 `ncm.apiBase`，如 `http://127.0.0.1:3000` |
| 酷狗 | 自建 [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi) 服务 | 插件配置 `kg.apiBase`，如 `http://127.0.0.1:4000` |

两个 API 服务我本人建议用 Docker 部署，同机部署注意端口 3000 / 4000

## WebUI

默认 `http://127.0.0.1:17818`，密码在插件配置 `webui.password`（留空则首次启动随机生成）。

功能：多音源搜索（单曲/歌单/专辑/歌手分栏，可试听、可投递到指定群）、扫码登录、调用统计（含失败原因聚合）、黑白名单、全量配置编辑、链接解析、运行日志。

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
| `评论 / 相似 / MV / 高潮 / 版本 [关键词]` | 歌曲扩展信息 |
| `专辑评论 / 歌单评论 / 相似歌单 / AI推荐 [关键词]` | 更多歌曲与歌单扩展 |
| `排行榜 [榜单名]` / `新歌 [地区]` / `歌手 X` / `歌手专辑 X` / `专辑 X` / `歌单 X` | 浏览发现 |
| `热搜` / `来首歌` / `日推` / `FM` / `新碟 [地区]` / `歌手榜` | 更多发现 |
| `MV榜 / 电台 / 热门歌手 / banner`（网易云）、`排行推荐 / 编辑精选 / 乐库 / 主题歌单 / 好歌精选`（酷狗）、`歌手MV / 相似歌手 / 猜你喜欢`（QQ） | 音源特色 |
| `订阅 日推` / `订阅新歌 歌手名` / `退订 日推 / 退订新歌 歌手名` / `我的订阅` | 订阅推送管理 |
| `排队 关键词` / `队列` / `切歌` | 点歌台队列 |
| `ncm登录 / kg登录 / qq登录` | 扫码登录（管理员） |
| `状态` / `ncm登出` 等 | 登录状态 / 退出 |
| `帮助` / `设置` / `音质 <档位>` / `统计` / `测试` / `WebUI` | 系统管理 |

所有指令支持 `#` 前缀（如 `#点歌 晴天`）。

## 配置（节选）

| 配置 | 默认 | 说明 |
|---|---|---|
| `defaultSource` | `auto` | 不带前缀点歌的默认音源；auto = 三平台混搜 |
| `ncm.apiBase` / `kg.apiBase` | 空 | 自建 API 服务地址，留空禁用对应音源 |
| `*.quality` | `auto` | 各音源最高音质，取不到自动降级 |
| `ncm.qualityUnblock` | true | VIP 歌 unblock 解灰兜底 |
| `sendVocal` / `uploadFile` | true | 语音 / 文件双通道 |
| `ffmpegCompress` | true | 发送失败压缩 mp3 重试（需系统 ffmpeg） |
| `acl.mode` | `off` | 黑白名单模式（WebUI 可视化管理） |
| `webui.port` | `17818` | 管理面板端口 |

完整配置在 WebUI「插件配置」页编辑。

## 平台支持

| 能力 | aiocqhttp | QQ官方 | Telegram | Kook/Discord | 微信个人 | 钉钉 |
|---|---|---|---|---|---|---|
| 文本/卡片 | 是 | 是 | 是 | 是 | 是 | 是（图片仅 http 链接） |
| 语音 | 是 | 是 (silk) | 是 | 是 | 否（转文件） | 否 |
| 群文件 | 是 | 是（>10MB 分片） | 是 | 是 | 是 | 否 |
| 原生音乐卡 | 是 | 否 | 否 | 否 | 否 | 否 |

钉钉等不支持本地图片/语音的平台，列表卡片自动回退纯文本，歌曲以直链文本投递。

## 致谢

- QQ 音乐接口：[qqmusic-api-python](https://github.com/L-1124/QQMusicApi)（GPLv3）
- 酷狗接口：自部署 [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（MIT）
- 网易云接口：自部署 [NeteaseCloudMusicApiEnhanced](https://github.com/neteasecloudmusicapienhanced/api-enhanced)(MIT)

项目仅供学习交流，请尊重版权，控制调用频率。

更新日志见 [CHANGELOG.md](CHANGELOG.md)。

## License

MIT，见 [LICENSE](LICENSE)。依赖的 qqmusic-api-python 为 GPLv3。
