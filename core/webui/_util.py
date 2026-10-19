"""WebUI 通用工具：JSON 响应等（独立模块避免 _server ↔ _api 循环导入）。"""

from __future__ import annotations

import asyncio
import json as _json_mod

from aiohttp import web

# body 读取超时（秒）：aiohttp 服务端没有内置请求读超时，慢速 POST（发完
# headers 后每 30s 滴 1 字节）可以无限期占住 conn_limit 的连接槽——64 个即可
# 让面板对全体请求 503。1MB 上限 body 在慢速移动网络下 15s 也足够
_BODY_READ_TIMEOUT = 15


def json_response(obj, status: int = 200) -> web.Response:
    return web.Response(
        text=_json_mod.dumps(obj, ensure_ascii=False),
        status=status,
        content_type="application/json",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def body_json(request: web.Request) -> dict:
    """POST body 解析成 dict；非 JSON / 非 dict 一律空 dict（调用方按缺参处理）。

    HTTPException 必须原样 re-raise：body 超限的 413 也是 HTTPException，
    吞成空 dict 会让登录等接口把「请求过大」误报成「密码错误」（401）。
    """
    try:
        data = await asyncio.wait_for(request.json(), _BODY_READ_TIMEOUT)
    except web.HTTPException:
        raise
    except asyncio.TimeoutError:
        # 超时按空 body 处理：调用方按缺参回 400/401，连接槽随 handler 返回释放
        return {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
