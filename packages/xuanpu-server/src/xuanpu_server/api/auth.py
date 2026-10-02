"""HTTP 接入层的认证：从请求里认出调用者（设计 6.1）。

::

    请求带 Authorization 头？
      是 → 根凭据 / API key（Platform.authenticate）
           └─ 另带 X-Xuanpu-Act-As 头 → 系统代员工调用（IdentityService.act_as）
      否 → 带登录 cookie（xuanpu_session）？
           是 → 写请求先过防跨站请求伪造的检查（identity.cookies）
                → 解析登录会话（IdentityService.authenticate_login）
           否 → 401

两种认证失败对外是同一个 ``401 unauthenticated``（「凭据无效」），不透露是哪一步没过；登录
cookie 认不出调用者时，错误响应另外清掉 cookie（见 ``api.app`` 的错误处理）。

路由函数的参数标注由 FastAPI 在运行时按路由所在模块解析，所以用 ``Caller`` 的模块要在运行时
导入它（不能放进 ``TYPE_CHECKING``）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request

from xuanpu_server.errors import AuthenticationError, PermissionDeniedError
from xuanpu_server.identity.cookies import LOGIN_COOKIE, check_cookie_request
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform

ACT_AS_HEADER = "X-Xuanpu-Act-As"
"""代员工调用时带的请求头，值是员工的平台用户标识。"""
_BEARER = "bearer "


def bearer(authorization: str | None) -> str:
    """取 ``Authorization: Bearer <凭据>`` 里的凭据；没有或不是 Bearer 一律按凭据无效。"""
    if authorization is None or not authorization.lower().startswith(_BEARER):
        raise AuthenticationError("凭据无效")
    return authorization[len(_BEARER) :].strip()


async def caller(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    act_as: Annotated[str | None, Header(alias=ACT_AS_HEADER)] = None,
) -> Principal:
    """认出调用者，顺序见模块说明。

    Raises:
        AuthenticationError: 没有凭据或凭据无效（登录 cookie 认不出时是它的子类
            ``LoginSessionInvalidError``）。
        PermissionDeniedError: 不允许的代员工调用（含用登录 cookie 带 ``X-Xuanpu-Act-As``）。
        CsrfRejectedError: 用登录 cookie 的写请求没过防跨站请求伪造的检查。
    """
    platform: Platform = request.app.state.platform
    if authorization is not None:
        return await platform.authenticate(bearer(authorization), act_as=act_as)
    token = request.cookies.get(LOGIN_COOKIE)
    if token is None:
        raise AuthenticationError("凭据无效")
    if act_as is not None:
        raise PermissionDeniedError(f"代员工调用只能用 API key，登录 cookie 不能带 {ACT_AS_HEADER}")
    # 先查请求的形状再查库：伪造的跨站请求不必走到查库这一步
    check_cookie_request(
        request.method, request.headers, public_origin=platform.identity.settings.public_url
    )
    return await platform.identity.authenticate_login(token)


Caller = Annotated[Principal, Depends(caller)]
"""路由参数的标注：经 ``caller`` 认出的调用者。"""


__all__ = ["ACT_AS_HEADER", "Caller", "bearer", "caller"]
