"""点歌 / 播放路由（聚合 + 三音源前缀别名）。"""

from __future__ import annotations

import re

from ..core.cards import build_versions_card_data
from ..core.errors import ApiError
from ..core.formatters import format_versions_text
from ..core.sources import token_to_source, word_to_source
from .actions import (
    _act_ai_recommend,
    _act_climax,
    _act_comment,
    _act_lyric,
    _act_lyric_word,
    _act_mv,
    _act_simi_playlist,
    _act_similar,
    _act_versions,
)
from .base import NO_LIST_HINT, Route

# 统一入口。空参形式（裸「点歌」「播放」）也要能匹配到用法提示：
# 用 (?:\s+(.+?))? 可选组而不是 \s+(.+?)，裸指令才不会整条不匹配、静默无响应
RE_REQUEST = r"^\s*[#＃]?\s*(?:点歌|音乐|music|mh)(?:\s+(.+?))?\s*$"
RE_PLAY = r"^\s*[#＃]?\s*(?:播放|play)(?:\s+(.+?))?\s*$"
# 音源前缀可选（#ncm 听1）：组 1 前缀、组 2 序号、组 3 音源后缀。
# 后缀组与前缀组保持同集（含 qqm）：缺了会让「听1 qqm」整条不匹配、静默无响应。
# 序号不限位数：原先 \d{1,9} 让 10 位以上的病态输入整条不匹配、静默无响应；
# 放开位数后统一由 handler 内「列表里没有第 N 首（共 M 首）」给出实际边界
RE_LISTEN_N = r"^\s*[#＃]?(?:(ncm|kg|qqm|qq)\s*)?听\s*(\d{1,})\s*(?:首)?\s*(ncm|kg|qqm|qq|网|狗|q)?\s*$"
RE_LISTEN_ALL = r"^\s*[#＃]?\s*听\s*所有\s*(?:首)?\s*$"
# 音源 token 可选：裸「换源」也要路由进来给用法提示（原先整条不匹配静默无
# 响应，run_switch 的用法分支成了死代码）
RE_SWITCH = r"^\s*[#＃]?\s*(?:换源|音源)(?:\s+(ncm|wy|kg|kugou|qq|qqm|网易云|酷狗|qq音乐))?\s*$"
# 音源前缀别名子命令排除词表：关键词命中任一词（子串包含）即不作为点歌关键词，
# run_src_request 返回 False 让位给对应子命令路由。新增子命令时在这里补词，
# 也要同步 handlers 里对应的正则。排除词必须与各 explore/account 路由的关键词
# 完全同步：漏词会让「#ncm 随机」这类输入被当成搜索关键词吞掉。
# 与原正则负向断言的等价性：断言 (?!(?:\S*\s*)?(?:词…)) 只检查首个空白段（\S*
# 允许跳进词中段，故「精品歌单」能靠子词「歌单」命中排除），子串包含在整个关键词
# 上做同样的包含检测，既有排除场景全部保留；差异是排除面更宽——关键词任意位置
# 含词都会排除（如「晴天 歌单版」不再作为点歌关键词），方向是更保守地让位，
# 只可能把边界输入让给子命令路由，不会把子命令消息吞成搜索。
_SRC_CMD_WORDS = tuple(
    # 词面与原正则交替段一致；MV/FM/play 等大小写变体统一小写（匹配前先 lower）。
    # 「AI\s*推荐」的空白弹性由 _hits_subcommand_word 的去空格比对单独兜住。
    # 与各路由正则的同步纪律覆盖三个模块：explore/account 的关键词、queue 的
    # 切歌族（跳过/下一首）、auth 的登出族（注销/解绑）、system 的 web ui、
    # 开关词（解析/卡片/语音/文件）与别名对（设置/配置，system 正则是双别名
    # 而词表只登记其一的话「#ncm 配置」会被吞成搜索）——漏词会让「#ncm 跳过」
    # 这类输入被当成搜索关键词真搜一首「跳过」
    "点歌 歌词 逐字 评论 相似 高潮 版本 mv 排行 新歌 歌手 "
    "专辑 歌单 热搜 来首 日推 fm 推荐 新碟 歌手榜 状态 登录 "
    "登出 注销 解绑 帮助 设置 配置 音质 api 统计 测试 听 音源 换源 "
    "喜欢 红心 点赞 取消 云盘 已购 最近 历史 关注列表 "
    "随机 来一首 放一首 好歌精选 热门精选 热门歌手 "
    "排队 点歌台 队列 切歌 跳过 下一首 清空队列 订阅 退订 我的订阅 订阅新歌 "
    "退订新歌 ai推荐 编辑精选 乐库 电台 猜你喜欢 搜索建议 banner "
    "解析 卡片 语音 文件 web 播放 play 音乐 music mh 菜单 帮助指令 指令".split()
)


def _hits_subcommand_word(keyword: str) -> bool:
    kw = keyword.lower()
    if not kw:
        return False
    # 「AI\s*推荐」的空白弹性用去空格比对保留（「AI推荐 / AI 推荐」都排除）
    return any(w in kw for w in _SRC_CMD_WORDS) or "ai推荐" in kw.replace(" ", "")


# 音源前缀别名，双分支（全角＃同认——\d 本就匹配全角数字，# 只认半角会不对称）：
# 1. 带 #：#ncm [点歌] 关键词（#ncm 晴天 / #ncm点歌 晴天）
# 2. 不带 #：音源词必须紧跟「点歌」（ncm点歌 晴天）——裸「qq」「qq 晴天」这类以
#    音源词开头的普通聊天不再触发点歌/三源搜索；子命令排除由代码词表判断
#    （_hits_subcommand_word），正则只负责取音源与关键词。
# 两组音源分别命名（src / src2）：同一 pattern 里命名组不可重名，handler 里取并集
RE_SRC_REQUEST = (
    r"^\s*[#＃](?P<src>ncm|kg|qqm|qq)\s*(?:点歌)?(?:\s+(?P<kw>.+?))?\s*$"
    r"|^\s*(?P<src2>ncm|kg|qqm|qq)\s*点歌(?:\s+(?P<kw2>.+?))?\s*$"
)


def _src_token(token: str | None) -> str:
    """听N 的音源后缀（网/狗/q…）。"""
    return token_to_source(token, default="")


async def run_switch(service, event):
    """换源重放：把最近播放的歌在指定音源重新找来播。"""
    reason = service.check_song_request()
    if reason:
        await service.reply(event, f"点歌不可用：{reason}")
        return
    m = re.search(RE_SWITCH, event.message_str, re.IGNORECASE)
    if not m:  # pragma: no cover - 本函数只由 RE_SWITCH 路由触发，理论不可达；保底防御
        return
    src = word_to_source(m.group(1))
    if not src:
        await service.reply(event, "用法：换源 ncm / 换源 kg / 换源 qq")
        return
    if not service.config.src_enabled(src):
        # 文案不带音源名：用户刚写下前缀，且与其他入口的「音源未配置」口径一致
        await service.reply(event, "音源未配置")
        return
    if reason := service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    try:
        await service.switch_source(event, src)
    except ApiError as e:
        service.release_cooldown(event)
        await service.reply(event, e.with_source())


async def _no_result_hint(service, event, keyword: str, notices: list[str] | None = None) -> None:
    """零结果回复：优先告知"某音源需扫码"，其次给搜索建议。

    notices 由本次搜索随结果一起返回——不能从 search 实例上取，
    那样并发搜索会互相取走对方的提示。
    """
    if notices:
        await service.reply(event, f"没有搜到「{keyword}」。\n" + "\n".join(notices))
        return
    sug = await service.suggest_for(keyword)
    if sug:
        await service.reply(event, f"没有搜到「{keyword}」。你是不是想搜：{' / '.join(sug[:5])}")
    else:
        await service.reply(event, f"没有搜到「{keyword}」相关的歌曲")


async def _song_request(service, event, keyword: str, forced_source: str = "auto"):
    reason = service.check_song_request()
    if reason:
        await service.reply(event, f"点歌不可用：{reason}")
        return
    keyword = (keyword or "").strip()
    if not keyword:
        await service.reply(event, "用法：点歌 关键词（ncm:/kg:/qq: 前缀可指定音源）")
        return
    if reason := service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    if forced_source == "auto":
        # 配置了具体默认音源就单源搜索；auto 才走三平台聚合
        forced_source = service.config.default_source
    if forced_source != "auto":
        try:
            songs, src, notices = await service.search_with_notices(keyword, forced_source)
        except ApiError as e:
            # 搜索没播出任何内容：退冷却，不让一个报错的关键词占住点歌间隔
            service.release_cooldown(event)
            await service.reply(event, e.with_source())
            return
        if not songs:
            service.release_cooldown(event)
            await _no_result_hint(service, event, keyword, notices)
            return
        tip = f"共 {len(songs)} 首"
        await service.list_to_session(event, keyword, songs, source=src, tip=tip)
        # 只出了列表、未播出任何媒体：退掉入口盖的冷却章，让章落在真正发歌的
        # 「听N/播放」出口（run_listen_n 的播放分支会重新 check_cooldown 盖章）。
        # 不退的话「点歌 晴天 → 听1」会被自己的冷却阻断，主流程不可用
        service.release_cooldown(event)
        return
    # 聚合模式：同名同歌手合并为一行，呈现多音源选择
    try:
        groups, _src, notices = await service.search_versions(keyword)
    except ApiError as e:
        service.release_cooldown(event)
        await service.reply(event, e.with_source())
        return
    if not groups:
        service.release_cooldown(event)
        # 零结果时优先展示聚合取数收集的登录提示（kg 禁匿名搜索是常态）
        await _no_result_hint(service, event, keyword, notices)
        return
    scope = service.scope(event)
    await service.sessions.set(scope, "versions", {"keyword": keyword, "songs": groups})
    data = build_versions_card_data(keyword, groups, tip=f"共 {len(groups)} 首 · 听N 播放 / 听N qq 指定音源")
    await service.reply_card_or_text(event, data, "list", "auto", format_versions_text)
    # 同上：列表出口退冷却章，让章随真正的播放出口重新落（「点歌 → 听N」主流程）
    service.release_cooldown(event)


async def run_request(service, event):
    m = re.search(RE_REQUEST, event.message_str, re.IGNORECASE)
    await _song_request(service, event, m.group(1) if m else "")


async def run_src_request(service, event):
    m = re.search(RE_SRC_REQUEST, event.message_str, re.IGNORECASE)
    if not m:
        return
    keyword = (m.group("kw") or m.group("kw2") or "").strip()
    if _hits_subcommand_word(keyword):
        # 关键词含子命令词：让位（return False 不吞事件）——子命令路由里优先级更低的
        # （如「听N」priority 4）要靠这里放行才能轮到，吞了它们就永远失响应。
        # 但词表是词面级的：子命令路由大多不带音源前缀组，「#kg 帮助」「#ncm 配置」
        # 让路后没有任何路由能接住整条消息（各路由正则锚定 ^）→ 死胡同静默。
        # 检测「除本路由外无任何路由能匹配完整消息」时当面给指引，不再沉默
        if not _other_route_can_take(event.message_str):
            await service.reply(
                event,
                f"「{event.message_str.strip()}」不是有效指令：音源前缀（#ncm/#kg/#qq）后面"
                # 例句必须是固定可路由词：keyword 本身可能只是 toggle 目标词/无路由词面
                # （如「帮助指令」「解析」），照搬会让指引推荐一条必然静默的指令
                "只能跟点歌关键词，子命令请去掉前缀直接发送，如「帮助」「听1」「设置」",
            )
            return
        return False
    await _song_request(service, event, keyword, token_to_source(m.group("src") or m.group("src2")))


def _other_route_can_take(message: str) -> bool:
    """除音源前缀别名路由外，是否还有任何路由的pattern能匹配这条消息。

    用与包装器相同的 re.search 语义在完整 message_str 上判定（子命令路由锚定
    ^，前缀不剥掉就匹配不上——这正是死胡同的成因）。"""
    from . import all_routes

    for r in all_routes():
        if r.name == "mh_src_request":
            continue
        # share 的链接解析路由没有 pattern（仅 event_message_type），且在 all_routes()
        # 里排第一——不做 None 防护则本函数每次调用都在第一轮 AttributeError，
        # 所有「音源前缀+子命令」输入（#ncm 听1 / #ncm 帮助）全部回「执行失败」
        if r.pattern is not None and r.pattern.search(message):
            return True
    return False


async def run_play(service, event):
    reason = service.check_song_request()
    if reason:
        await service.reply(event, f"点歌不可用：{reason}")
        return
    m = re.search(RE_PLAY, event.message_str, re.IGNORECASE)
    keyword = (m.group(1) or "").strip() if m else ""
    if not keyword:
        # 裸「播放」没有可播的目标：与 _song_request 空参同口径给用法
        await service.reply(event, "用法：播放 关键词，或点歌后用「听N」")
        return
    if reason := service.check_cooldown(event):
        await service.reply(event, f"⏳ {reason}")
        return
    try:
        songs, _ = await service.search_songs(keyword, service.config.default_source, limit=1)
    except ApiError as e:
        # 搜索失败也退冷却：本次没有产出任何播放内容
        service.release_cooldown(event)
        await service.reply(event, e.with_source())
        return
    if not songs:
        service.release_cooldown(event)
        await service.reply(event, f"没有搜到「{keyword}」相关的歌曲")
        return
    await service.play_song(event, songs[0])


async def run_listen_n(service, event):
    # 会话仲裁先于点歌开关检查：列表若是其他音乐插件产出，即使本插件点歌开关关闭
    # 也要让路（return False 不吞事件）——先查开关会把「听1」吞成本插件的
    # 「点歌不可用」，跨插件失去让路
    scope = service.scope(event)
    if not await service.sessions.owns_scope(scope):
        return False
    if reason := service.check_song_request():
        # 总开关已由路由 gated 静默让路，走到这里只会是点歌开关关闭：与点歌同口径当面提示
        await service.reply(event, f"点歌不可用：{reason}")
        return
    m = re.search(RE_LISTEN_N, event.message_str, re.IGNORECASE)
    if not m:
        return
    try:
        n = int(m.group(2))
    except ValueError:
        # CPython ≥3.10.7 对超 4300 位的数字串 int() 直接抛（正则 \d{1,} 不限位数）：
        # 真实列表到不了这个量级，按远超上界处理走「没有第 N 首」边界反馈
        n = 10**9
    # 音源后缀 > 音源前缀（「#ncm 听1 qq」以后缀为准；只写前缀时前缀即意图）。
    # 前缀组为 None 时不能走 token_to_source——它对 None 返回 "auto"，会误触换源分支
    want_src = _src_token(m.group(3))
    if not want_src and m.group(1):
        want_src = token_to_source(m.group(1))
    song, action, list_kw = await service.take_action_target(event, n)
    if song is None:
        # 能走到这里说明 scope 归属本插件（别人的列表在 owns_scope 已让路），
        # 无列表 / 越界都给反馈，与「听所有」的空列表提示对齐
        songs = await service.sessions.songs_of(scope)
        if songs:
            if n <= 0:
                # 正则允许「听000」：序号语义从 1 起，给可用的指引
                await service.reply(event, f"序号从 1 开始（列表共 {len(songs)} 首）")
            else:
                # 超过列表长度的「听1000」也落在这里：带上共 M 首的实际边界
                await service.reply(event, f"列表里没有第 {n} 首（共 {len(songs)} 首）")
        else:
            await service.reply(event, NO_LIST_HINT)
        return

    async def _restore_action() -> None:
        # take_action_target 已原子清掉 pending action：下面冷却/节流拒绝时写回
        # 会话，否则用户「歌词 晴天 → 听1」撞上冷却后，动作永久丢失——冷却过后
        # 的「听1」会退化成播放且无任何提示。CAS 比对消费时的列表关键词：窗口里
        # 他人「点歌 新词」整包替换列表后，旧动作不落到新列表上
        if action:
            try:
                await service.sessions.update_action_if_keyword(scope, action, list_kw)
            except Exception as e:  # noqa: BLE001 - 写回失败只影响下次意图，不顶替冷却提示
                service.log_warn(f"听N 动作写回失败: {e}")

    # 多音源分组：二段式动作（「歌词 → 听1」）优先于播放——分组本身不是歌曲，
    # 把动作目标解析成主版本继续走动作分派；否则动作被播放分支静默消费，
    # 插件自己承诺的「回复 听N 查看歌词」直接失效。明确指定音源（听1 qq）或
    # 无动作时仍是播放意图，保持 play_group
    if song.get("versions") and action and not want_src:
        primary = song.get("primary")
        if not isinstance(primary, dict):
            versions = song.get("versions") or []
            primary = versions[0] if versions else None
        if primary:
            song = dict(primary)
    if song.get("versions"):
        if reason := service.check_cooldown(event):
            await _restore_action()
            await service.reply(event, f"⏳ {reason}")
            return
        try:
            result = await service.play_group(event, song, source=want_src)
        except ApiError as e:
            await _restore_action()
            service.release_cooldown(event)
            await service.reply(event, e.with_source())
            return
        # no_version / 全部换源失败都没播出内容：退还冷却（成功路径 play_song 自行持有）
        if not result.get("ok"):
            await _restore_action()
            service.release_cooldown(event)
        return
    if want_src and not song.get("kind"):
        # 普通列表 + 指定音源：按歌名歌手在该音源重搜
        if reason := service.check_cooldown(event):
            await _restore_action()
            await service.reply(event, f"⏳ {reason}")
            return
        try:
            await service.switch_source_for(event, song, want_src)
        except ApiError as e:
            await _restore_action()
            service.release_cooldown(event)
            await service.reply(event, e.with_source())
        return
    # 直接播放兜底（无 kind 无动作）：只吃点歌冷却——连播「听1听2」是既有 UX，
    # 不引入 browse 节流（与原口径一致）
    if not song.get("kind") and not action:
        if reason := service.check_cooldown(event):
            await service.reply(event, f"⏳ {reason}")
            return
        try:
            await service.play_song(event, song)
        except ApiError as e:
            service.release_cooldown(event)
            await service.reply(event, e.with_source())
        return
    # 动作/候选项展开分支：与「歌词/评论/排行榜」直连指令（throttle=2.0）同一批
    # 上游接口，同样过 browse 轻频控——否则「评论 晴天 → 听1听2听3…」可以绕过
    # 频控连刷高风控接口
    if reason := service.check_browse(event, 2.0):
        await _restore_action()
        await service.reply(event, f"⏳ {reason}")
        return
    # 候选项展开（专辑/歌单/榜单分类/MV/歌手候选）——产出新列表不发歌，不占点歌冷却
    if song.get("kind") in ("album", "playlist", "rank", "mv", "artist"):
        from .explore_common import expand_candidate

        if want_src:
            # 候选项只能按其原音源展开（榜单/专辑本身挂在特定平台下），
            # 显式指定的音源无法套用——明说而不是无声丢弃
            await service.reply(event, "候选项不支持指定音源，已按原音源展开")
        try:
            handled = await expand_candidate(service, event, song, n)
        except ApiError as e:
            # 与冷却/节流拒绝同一纪律：take_action_target 已清掉 pending action，
            # 展开失败要写回，否则动作永久丢失
            await _restore_action()
            await service.reply(event, e.with_source())
            return
        if not handled:
            await _restore_action()
            await service.reply(event, "该候选项没有可展开的内容")
        return
    if action == "lyric":
        await _act_lyric(service, event, song)
    elif action == "lyric_word":
        await _act_lyric_word(service, event, song)
    elif action == "comment":
        await _act_comment(service, event, song)
    elif action == "similar":
        await _act_similar(service, event, song)
    elif action == "simi_playlist":
        await _act_simi_playlist(service, event, song)
    elif action == "mv":
        await _act_mv(service, event, song)
    elif action == "climax":
        await _act_climax(service, event, song)
    elif action == "versions":
        await _act_versions(service, event, song)
    elif action == "ai_recommend":
        await _act_ai_recommend(service, event, song)
    else:
        # 未知动作兜底：按播放处理，与其他发歌出口同一频控（盖章后未产出由 play_song/兜底退章）
        if reason := service.check_cooldown(event):
            await _restore_action()
            await service.reply(event, f"⏳ {reason}")
            return
        try:
            await service.play_song(event, song)
        except ApiError as e:
            await _restore_action()
            service.release_cooldown(event)
            await service.reply(event, e.with_source())


async def run_listen_all(service, event):
    # 与 run_listen_n 一致：会话仲裁先于点歌开关检查，别人的列表先让路不吞事件
    scope = service.scope(event)
    if not await service.sessions.owns_scope(scope):
        return False
    if reason := service.check_song_request():
        # 仅点歌开关关闭时当面提示（总开关由路由 gated 静默让路）
        await service.reply(event, f"点歌不可用：{reason}")
        return
    songs = await service.sessions.songs_of(scope)
    if not songs:
        await service.reply(event, NO_LIST_HINT)
        return
    # 会话列表里混着候选条目（专辑/歌单/榜单/MV/歌手，只有 kind+id 没有 sid）：
    # 直接连播会在 song_url_best 上逐首失败并连发错误卡。只保留可播放条目
    # （多音源分组有 versions，普通歌曲有 sid/sid2），其余跳过并说明。
    playable = [s for s in songs if s.get("versions") or s.get("sid") or s.get("sid2")]
    if not playable:
        await service.reply(event, "当前列表是候选列表（专辑/歌单/榜单等），请回复 听N 逐个播放")
        return
    if reason := service.check_cooldown(event):
        # 与其他发歌出口同一频控；空列表 / 纯候选列表的路径没有盖到章，无需退
        await service.reply(event, f"⏳ {reason}")
        return
    await service.play_all(event, playable)


# ──────────── 路由表 ────────────
def routes() -> list[Route]:
    rs = [
        # 优先级 6：音源前缀别名先于统一入口（#ncm点歌 不会被统一入口抢走）
        Route(
            re.compile(RE_SRC_REQUEST, re.IGNORECASE),
            "mh_src_request",
            "指定音源点歌",
            run_src_request,
            priority=6,
        ),
        Route(
            re.compile(RE_REQUEST, re.IGNORECASE),
            "mh_request",
            "点歌（默认音源可配）",
            run_request,
            priority=5,
        ),
        Route(re.compile(RE_PLAY, re.IGNORECASE), "mh_play", "直接播放", run_play, priority=5),
        Route(
            re.compile(RE_LISTEN_ALL, re.IGNORECASE),
            "mh_listen_all",
            "播放整个列表",
            run_listen_all,
            priority=4,
        ),
        Route(
            re.compile(RE_LISTEN_N, re.IGNORECASE),
            "mh_listen_n",
            "播放列表中的第 N 首",
            run_listen_n,
            priority=4,
        ),
        Route(
            re.compile(RE_SWITCH, re.IGNORECASE),
            "mh_switch",
            "换源重放当前歌曲",
            run_switch,
            priority=5,
        ),
    ]
    # 总开关关闭 → 包装器静默让路；点歌开关关闭 → 体内 check_song_request 当面提示
    for r in rs:
        r.gated = True
    return rs
