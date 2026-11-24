"""登录相关接口共用的响应助手：把登录流程一步的结果翻成 302，以及有上限地读请求体。

``api.identity`` 与 ``api.desktop_login`` 共用；单独成模块，免得两者互相导入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import RedirectResponse

from server.domains.identity.cookies import (
    clear_login_attempt_cookie,
    set_login_attempt_cookie,
    set_login_cookie,
)
from server.domains.identity.login import LOGIN_ATTEMPT_TTL_SECONDS

if TYPE_CHECKING:
    from fastapi import Request

    from server.domains.identity.login import LoginRedirect


def login_response(
    outcome: LoginRedirect, *, secure: bool, clear_attempt: bool
) -> RedirectResponse:
    """把登录流程一步的结果翻成 302：按需写登录尝试 cookie、写登录 cookie、清登录尝试 cookie。

    响应不进缓存（``Cache-Control: no-store``）：里面有一次性的 cookie 与跳转。
    """
    response = RedirectResponse(
        outcome.location, status_code=302, headers={"Cache-Control": "no-store"}
    )
    if outcome.attempt_cookie is not None:
        set_login_attempt_cookie(
            response, outcome.attempt_cookie, max_age=LOGIN_ATTEMPT_TTL_SECONDS, secure=secure
        )
    elif clear_attempt:
        clear_login_attempt_cookie(response, secure=secure)
    if outcome.session_token is not None:
        set_login_cookie(
            response, outcome.session_token, max_age=outcome.session_max_age, secure=secure
        )
    return response


async def read_bounded(request: Request, limit: int) -> bytes:
    """读请求体，读到超过 ``limit`` 就停：返回的最多 ``limit + 1`` 字节，超没超限由调用方判断。"""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            break
    return b"".join(chunks)[: limit + 1]


__all__ = ["login_response", "read_bounded"]
