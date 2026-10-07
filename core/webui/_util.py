"""WebUI 通用工具：JSON 响应等（独立模块避免 _server ↔ _api 循环导入）。"""

from __future__ import annotations

import json as _json_mod

from aiohttp import web


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
        data = await request.json()
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}
