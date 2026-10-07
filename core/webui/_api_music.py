"""WebUI REST API handlers（账号 / 扫码 / 多音源搜索 / 链接解析 / 服务检查）。"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

from aiohttp import web
from astrbot.api import logger

from .. import SOURCE_NAMES, SOURCES
from ..errors import ApiError, TooManyTasks
from ._util import body_json as _body
from ._util import json_response as _json

TAG = "[music_hub]"


def _host_of(url: str) -> str:
    """只回显 URL 的 host 部分。

    展开后的短链常带签名 token（?sign=...&t=...），原样回显等于把凭证送到浏览器日志里。
    """
    try:
        parts = urlsplit(url.strip())
        return f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.scheme else url[:200]
    except ValueError:
        return url[:200]


# ──────────── 账号 ────────────
def make_accounts(server):
    """三平台状态并行查询。串行时最坏 9 次上游往返，任一平台挂起拖住整页。"""

    async def _safe(coro, default):
        try:
            return await coro
        except Exception:  # noqa: BLE001 - 单项失败只降级该项
            return default

    async def _row(service, src: str) -> dict:
        row = {
            "source": src,
            "name": SOURCE_NAMES[src],
            "enabled": server.config.src_enabled(src),
            "loggedIn": False,
            "nickname": "",
            "uid": "",
            "avatar": "",
            "quality": server.config.src_quality(src),
        }
        if not row["enabled"]:
            row["nickname"] = "未配置 API"
            return row
        status, vip, grade = await asyncio.gather(
            _safe(service.client_of(src).login_status(), {"loggedIn": False, "nickname": "查询失败"}),
            _safe(service.vip_summary(src), ""),
            _safe(service.grade_summary(src), ""),
        )
        row.update(status if isinstance(status, dict) else {})
        if grade:
            row["vip"] = f"{vip} · {grade}" if vip else grade
        elif vip:
            row["vip"] = vip
        return row

    async def handler(request: web.Request) -> web.Response:
        service = server.service
        rows = await asyncio.gather(*(_row(service, src) for src in SOURCES))
        return _json({"accounts": list(rows)})

    return handler


def make_account_logout(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        src = str(body.get("source", ""))
        if src not in SOURCES:
            return _json({"error": "未知音源"}, 400)
        try:
            if src == "qq":
                await server.service.qq.logout()
            else:
                client = server.service.client_of(src)
                if hasattr(client, "logout"):
                    await client.logout()
        except Exception:  # noqa: BLE001
            pass
        server.config.clear_src_cookie(src)
        saved = await server.config.save_async()
        return _json({"ok": True, "saved": saved})

    return handler


def make_account_refresh(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        src = str(body.get("source", ""))
        if src != "qq":
            return _json({"error": "仅 QQ 音乐支持凭证刷新（其余平台登录态长期有效）"}, 400)
        ok = await server.service.qq.refresh_credential()
        return _json({"ok": ok, "msg": "刷新成功" if ok else "刷新失败（可能需要重新扫码）"})

    return handler


def make_account_cookie(server):
    """手动粘贴 cookie / 凭证。"""

    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        src = str(body.get("source", ""))
        value = str(body.get("value", "")).strip()
        if src not in ("ncm", "kg", "qq"):
            return _json({"error": "未知音源"}, 400)
        if not value:
            return _json({"error": "内容不能为空"}, 400)
        if src == "qq":
            import json as _json_mod

            try:
                data = _json_mod.loads(value)
                if not isinstance(data, dict) or not data.get("musickey"):
                    return _json({"error": "QQ 凭证需为含 musickey 的 JSON"}, 400)
            except ValueError:
                return _json({"error": "QQ 凭证需为 JSON（扫码登录可自动获取）"}, 400)
        server.config.set_src_cookie(src, value)
        saved = await server.config.save_async()
        return _json({"ok": True, "saved": saved})

    return handler


# ──────────── 扫码登录 ────────────
def make_qr_start(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        src = str(body.get("source", ""))
        # login_type 只有 qq 用得上（ncm/kg 是 key-based 扫码），但 LoginFlows.start
        # 对三者都接受该参数，故直接透传，不做按平台的无效三元
        login_type = str(body.get("type", "qq"))
        if src not in SOURCES:
            return _json({"error": "未知音源"}, 400)
        # 同源旧会话先取消：active_of + gc 的组合清不掉刚开的待扫码会话，
        # 反复点扫码会累积轮询任务（LoginFlows.start 内部也会做一次，这里是幂等的）
        await server.service.login.cancel_source(src)
        await server.service.login.gc()
        try:
            session = await server.service.login.start(src, login_type)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{TAG} 创建扫码会话失败（{src}）: {e}")
            return _json({"error": "创建扫码会话失败，请稍后重试"}, 500)
        return _json(
            {
                "ticket": session.ticket,
                "qr": session.snapshot().get("qr", ""),
                "qrUrl": session.qr_url,
                "source": src,
            }
        )

    return handler


def make_qr_status(server):
    async def handler(request: web.Request) -> web.Response:
        ticket = request.query.get("ticket", "")
        snap = await server.service.login.status(ticket)
        if snap is None:
            return _json({"error": "会话不存在或已过期"}, 404)
        return _json(snap)

    return handler


def make_qr_cancel(server):
    async def handler(request: web.Request) -> web.Response:
        body = await _body(request)
        await server.service.login.cancel(str(body.get("ticket", "")))
        return _json({"ok": True})

    return handler


# ──────────── 多音源搜索（WebUI 面板） ────────────
def make_search(server):
    async def handler(request: web.Request) -> web.Response:
        service = server.service
        keyword = (request.query.get("keyword") or "").strip()
        if not keyword:
            return _json({"error": "keyword 不能为空"}, 400)
        type_ = (request.query.get("type") or "song").lower()
        if type_ not in ("song", "playlist", "album", "artist"):
            type_ = "song"
        try:
            if type_ == "song":
                groups, src = await service.search_versions(keyword)
                for g in groups:
                    for v in g.get("versions", []):
                        server.cache_song(v)
                return _json(
                    {
                        "keyword": keyword,
                        "type": "song",
                        "source": src,
                        "groups": [
                            {
                                "index": g.get("index", i + 1),
                                "name": g.get("name", ""),
                                "artist": g.get("artist", ""),
                                "album": g.get("album", ""),
                                "cover": g.get("cover", ""),
                                "duration": g.get("duration", ""),
                                "versions": [
                                    {
                                        "source": v.get("source", ""),
                                        "sourceName": SOURCE_NAMES.get(v.get("source", ""), ""),
                                        "sid": v.get("sid", ""),
                                        "pay": bool(v.get("pay")),
                                        "trial": bool(v.get("trial")),
                                        "album": v.get("album", ""),
                                    }
                                    for v in g.get("versions", [])
                                ],
                            }
                            for i, g in enumerate(groups)
                        ],
                    }
                )
            items = await service.search_multi(keyword, type_, 10)
            return _json(
                {
                    "keyword": keyword,
                    "type": type_,
                    "source": "auto",
                    "items": [
                        {
                            "index": it.get("index", i + 1),
                            "kind": type_,
                            "id": it.get("id", ""),
                            "name": it.get("name", ""),
                            "sub": it.get("creator") or it.get("artist") or "",
                            "cover": it.get("cover", ""),
                            "duration": it.get("duration", ""),
                            "source": it.get("source", ""),
                            "sourceName": SOURCE_NAMES.get(it.get("source", ""), ""),
                            "count": it.get("trackCount") or it.get("songCount") or 0,
                        }
                        for i, it in enumerate(items)
                    ],
                }
            )
        except ApiError as e:
            return _json({"error": e.user_msg()}, 502)
        except Exception as e:  # noqa: BLE001
            # 原始异常字符串可能带上游 API 地址与内部 code，不回显浏览器；
            # 详情只进服务端日志（前端已做 esc，无 XSS，但会泄露内部信息）
            logger.warning(f"{TAG} 上游调用失败：{e}")
            return _json({"error": "上游服务异常，请稍后重试"}, 502)

    return handler


# ──────────── 试听 / 远程投递 / 历史 / 日志 ────────────
def make_preview(server):
    async def handler(request: web.Request) -> web.Response:
        source = request.query.get("source", "")
        sid = request.query.get("sid", "")
        song = server.get_song(source, sid)
        if not song:
            return _json({"error": "歌曲缓存已过期，请重新搜索"}, 404)
        try:
            play = await server.service.resolve_play(song)
        except ApiError as e:
            return _json({"error": e.user_msg()}, 502)
        except Exception as e:  # noqa: BLE001
            # 原始异常字符串可能带上游 API 地址与内部 code，不回显浏览器；
            # 详情只进服务端日志（前端已做 esc，无 XSS，但会泄露内部信息）
            logger.warning(f"{TAG} 上游调用失败：{e}")
            return _json({"error": "上游服务异常，请稍后重试"}, 502)
        return _json({"url": play.get("url", ""), "label": play.get("label") or play.get("qualityLabel", "")})

    return handler


def make_remote_scopes(server):
    async def handler(request: web.Request) -> web.Response:
        return _json({"scopes": server.service.umo_rows()})

    return handler


def is_known_umo(umo: str, rows) -> bool:
    """umo 是否在插件已登记的会话列表里（远程投递白名单）。

    只判「含冒号」会让已认证会话能向任意构造的 unified_msg_origin 投递消息
    （包括插件从未接触过的群）。rows 来自 umo_rows()。
    """
    if not umo or ":" not in umo:
        return False
    return any(r.get("umo") == umo for r in rows or () if isinstance(r, dict))


def make_remote_play(server):
    async def handler(request: web.Request) -> web.Response:
        service = server.service
        body = await _body(request)
        umo = str(body.get("umo", ""))
        source = str(body.get("source", ""))
        sid = str(body.get("sid", ""))
        song = server.get_song(source, sid)
        if not song:
            return _json({"error": "歌曲缓存已过期，请重新搜索"}, 404)
        if not is_known_umo(umo, service.umo_rows()):
            return _json({"error": "请选择要投递的会话"}, 400)
        try:
            service.spawn(service.remote_play(umo, song))
        except TooManyTasks as e:
            return _json({"error": str(e)}, 429)
        return _json({"ok": True, "queued": True, "name": song.get("name", "")})

    return handler


def make_history(server):
    async def handler(request: web.Request) -> web.Response:
        rows = server.service.history_all()
        out = [
            {
                "scope": r["scope"],
                "items": [
                    {
                        "name": it.get("name", ""),
                        "artist": it.get("artist", ""),
                        "source": it.get("source", ""),
                        "ts": it.get("ts", 0),
                    }
                    for it in r["items"][-10:]
                ],
            }
            for r in rows[:30]
        ]
        return _json({"history": out})

    return handler


def make_logs(server):
    async def handler(request: web.Request) -> web.Response:
        buf = getattr(server.service, "log_buffer", None)
        if buf is None:
            return _json({"entries": [], "seq": 0})
        try:
            after = int(request.query.get("after", "0") or 0)
        except ValueError:
            after = 0
        entries, seq = buf.after(after, 200)
        return _json({"entries": entries, "seq": seq})

    return handler


# ──────────── 链接解析工具（WebUI 面板） ────────────
def make_resolve(server):
    async def handler(request: web.Request) -> web.Response:
        service = server.service
        body = await _body(request)
        text = str((body or {}).get("text", "")).strip()
        if not text:
            return _json({"error": "请粘贴分享链接或文本"}, 400)
        from ..resolve import (
            HINTS,
            expand_short_links,
            extract_kg_target,
            extract_ncm_target,
            extract_qq_target,
        )

        expanded = await expand_short_links(text)
        hits = [src for src in SOURCES if HINTS[src].search(expanded)]
        # expanded 可能带签名 token，回显前只保留 host 部分
        result = {
            "source": "",
            "type": "",
            "name": "",
            "matched": hits,
            "expanded": _host_of(expanded),
        }
        try:
            if "ncm" in hits:
                result["source"] = "ncm"
                target = extract_ncm_target(expanded)
                if target:
                    kind, tid = target
                    if kind == "playlist":
                        result["type"] = "歌单"
                        pl, songs = await service.ncm_playlist_songs(tid)
                        result["name"] = pl.get("name", "")
                        result["count"] = len(songs)
                    elif kind == "album":
                        result["type"] = "专辑"
                        detail = await service.ncm.album_detail(tid)
                        result["name"] = (detail.get("album") or {}).get("name", "")
                        result["count"] = len(detail.get("songs") or [])
                    else:
                        result["type"] = "歌曲"
                        lst = await service.ncm.song_detail([tid])
                        if lst:
                            result["name"] = f"{lst[0].get('name')} - {lst[0].get('artist')}"
            elif "kg" in hits:
                result["source"] = "kg"
                target = extract_kg_target(expanded)
                if target and target[0] == "song":
                    result["type"] = "歌曲"
                    song = await service.kg.audio_by_hash(target[1])
                    if song:
                        result["name"] = f"{song.get('name')} - {song.get('artist')}"
            elif "qq" in hits:
                result["source"] = "qq"
                target = extract_qq_target(expanded)
                if target:
                    kind, value = target
                    result["type"] = {"song": "歌曲", "album": "专辑", "playlist": "歌单"}[kind]
                    if kind == "song":
                        song = await service.qq.song_detail(value)
                        if song:
                            result["name"] = f"{song.get('name')} - {song.get('artist')}"
        except Exception as e:  # noqa: BLE001
            result["error"] = e.user_msg() if isinstance(e, ApiError) else "投递失败，请稍后重试"
        if result.get("name"):
            result["playable"] = True
        return _json(result)

    return handler


# ──────────── 服务检查 ────────────
def make_service_check(server):
    async def handler(request: web.Request) -> web.Response:
        service = server.service

        async def check_ncm():
            try:
                await service.ncm.request("/song/url/v1", {"id": 1, "level": "standard"})
                return {"ok": True, "msg": "可用"}
            except ApiError as e:
                return {"ok": False, "msg": e.user_msg()}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 服务检查 ncm 失败: {e}")
                return {"ok": False, "msg": "无法连接服务"}

        async def check_kg():
            try:
                await service.kg.request("/search/hot", {})
                return {"ok": True, "msg": "可用"}
            except ApiError as e:
                return {"ok": False, "msg": e.user_msg()}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 服务检查 kg 失败: {e}")
                return {"ok": False, "msg": "无法连接服务"}

        async def check_qq():
            try:
                from ..api.qq import available as qq_ok

                if not qq_ok():
                    return {"ok": False, "msg": "未安装 qqmusic-api-python"}
                status = await service.qq.login_status()
                return {"ok": True, "msg": "已登录" if status.get("loggedIn") else "匿名可用"}
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{TAG} 服务检查 qq 失败: {e}")
                return {"ok": False, "msg": "库调用失败"}

        ncm, kg, qq = await asyncio.gather(check_ncm(), check_kg(), check_qq(), return_exceptions=True)

        def _safe(r):
            if isinstance(r, BaseException):
                logger.warning(f"{TAG} 服务检查异常: {r}")
                return {"ok": False, "msg": "检查项异常"}
            return r

        return _json(
            {
                "ncm": _safe(ncm),
                "kg": _safe(kg),
                "qq": _safe(qq),
                "checkedAt": int(time.time()),
            }
        )

    return handler
