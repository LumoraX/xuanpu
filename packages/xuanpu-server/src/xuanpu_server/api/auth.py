"""HTTP 接入层的认证：从请求里认出调用者（设计 6.1，另有加固）。

::

    请求带 Authorization 头？
      是 → 根凭据 / API key（Platform.authenticate）
           └─ 另带 X-Xuanpu-Act-As 头 → 系统代员工调用（IdentityService.act_as）
      否 → 配置了 public_url，且带登录 cookie？
           是 → 先查请求的形状（不查库）：同名 cookie 只能有一个（否则 401）、
                不能带 X-Xuanpu-Act-As（400）、写请求过防跨站请求伪造的检查（403）
                → 再验会话（IdentityService.authenticate_login）
           否 → 401

没有配置 ``public_url`` 时不认登录 cookie（按没有凭据处理），这样认 cookie 时自定义头与
``Origin`` 两道检查总是都生效（见 ``identity.cookies``）。

认证失败对外是同一个 ``401 unauthenticated``（「凭据无效」），不透露是哪一步没过；验会话没过
（会话失效、员工或租户停用）时，错误响应另外清掉 cookie（见 ``api.app`` 的错误处理）。另两种
401 不清 cookie：同名 cookie 不止一个时，清掉会让往父域塞 cookie 的人能持续把员工踢下线；没配
``public_url`` 时，这个部署根本不签发 cookie，收到的不是它发的就是配置改动之前留下的——不清，
配置恢复后已有的登录会话还能接着用（会话照常按有效期失效）。

路由函数的参数标注由 FastAPI 在运行时按路由所在模块解析，所以用 ``Caller`` 的模块要在运行时
导入它（不能放进 ``TYPE_CHECKING``）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Header, Request

from xuanpu_server.errors import AuthenticationError, InvalidRequestError
from xuanpu_server.identity.cookies import (
    check_cookie_request,
    login_cookie_name,
    read_login_cookie,
)
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
        AuthenticationError: 没有凭据或凭据无效（验会话没过时是它的子类
            ``LoginSessionInvalidError``）。
        InvalidRequestError: 用登录 cookie 却带了 ``X-Xuanpu-Act-As``。
        PermissionDeniedError: 不允许的代员工调用。
        CsrfRejectedError: 用登录 cookie 的写请求没过防跨站请求伪造的检查。
    """
    platform: Platform = request.app.state.platform
    if authorization is not None:
        return await platform.authenticate(bearer(authorization), act_as=act_as)
    settings = platform.identity.settings
    public_url = settings.public_url
    if public_url is None:
        # 没配 public_url 不认 cookie，按没有凭据处理
        raise AuthenticationError("凭据无效")
    # 同名 cookie 不止一个时这里就报 401
    name = login_cookie_name(settings.cookie_secure)
    token = read_login_cookie(request.headers.getlist("cookie"), name=name)
    if token is None:
        raise AuthenticationError("凭据无效")
    # 先查请求的形状再验会话：形状不对的请求（包括伪造的跨站请求）不必走到查库那一步
    if act_as is not None:
        raise InvalidRequestError(f"用登录 cookie 时不能带 {ACT_AS_HEADER}：代员工调用只给 API key")
    check_cookie_request(request.method, request.headers, public_origin=public_url)
    return await platform.identity.authenticate_login(token)


Caller = Annotated[Principal, Depends(caller)]
"""路由参数的标注：经 ``caller`` 认出的调用者。"""


__all__ = ["ACT_AS_HEADER", "Caller", "bearer", "caller"]
