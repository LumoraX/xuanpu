"""浏览器登录的 cookie，与 cookie 认证请求的防跨站请求伪造检查（设计 6.2）。

**cookie**：名字 ``xuanpu_session``，值是 32 字节随机数（URL 安全的 base64）；HttpOnly、
SameSite=Lax、Path=/；默认 Secure，本地用 HTTP 开发时可以经 ``identity.cookie_secure`` 关掉。
平台只存令牌的 SHA-256 摘要，令牌本身不落库。

**防跨站请求伪造**：用 cookie 认证的写请求（GET、HEAD、OPTIONS 以外的方法）必须同时满足：

1. 带 ``X-Requested-By: xuanpu`` 头。平台不开放带凭据的跨域（不发 CORS 头），跨站页面的
   脚本发不出带自定义头的请求，表单与导航又带不了自定义头——这一条本身就挡住了跨站请求伪造；
2. 请求带了 ``Origin`` 的，它必须等于 ``public_url`` 的源。这是第二道防线。``public_url``
   没有配置时没有可比的源，这一条不查，只靠第 1 条；启用飞书登录必须配置 ``public_url``（登录
   流程按它拼回调地址），所以正式部署里两条都生效。

不满足的返回 ``403 csrf_rejected``。用 API key（``Authorization`` 头）认证的请求不受这些限制：
浏览器不会自动带上 ``Authorization``，跨站请求伪造无从谈起。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from xuanpu_server.errors import CsrfRejectedError
from xuanpu_server.tenancy import hash_secret

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.responses import Response

LOGIN_COOKIE = "xuanpu_session"
"""登录 cookie 的名字。"""
REQUESTED_BY_HEADER = "X-Requested-By"
"""cookie 认证的写请求必须带的头。"""
REQUESTED_BY_VALUE = "xuanpu"
"""``X-Requested-By`` 头必须等于的值。"""
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""不改状态的方法：cookie 认证时不做防跨站请求伪造的检查。"""


def new_login_token() -> tuple[str, str]:
    """签发一个登录令牌，返回 ``(令牌, 摘要)``。令牌只写进 cookie，库里只存摘要。"""
    token = secrets.token_urlsafe(32)
    return token, login_token_hash(token)


def login_token_hash(token: str) -> str:
    """登录令牌的摘要（SHA-256 十六进制）。令牌是高熵随机串，不需要慢哈希。"""
    return hash_secret(token)


def set_login_cookie(response: Response, token: str, *, max_age: int, secure: bool) -> None:
    """把登录令牌写进 cookie。属性见模块说明；``max_age`` 与登录会话的有效期一致。"""
    response.set_cookie(
        LOGIN_COOKIE,
        token,
        max_age=max_age,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def clear_login_cookie(response: Response, *, secure: bool) -> None:
    """清掉浏览器里的登录 cookie。属性要与写入时相同，浏览器才认作同一个 cookie。"""
    response.delete_cookie(LOGIN_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")


def check_cookie_request(
    method: str, headers: Mapping[str, str], *, public_origin: str | None
) -> None:
    """对一个用 cookie 认证的请求做防跨站请求伪造的检查（规则见模块说明）。

    Args:
        method: 请求方法。
        headers: 请求头（键不区分大小写的映射，如 Starlette 的 ``Headers``）。
        public_origin: 平台对外地址的源（``public_url``，配置加载时已规整为
            ``scheme://host[:port]``）；None 表示没有配置，不查 ``Origin``。

    Raises:
        CsrfRejectedError: 写请求缺 ``X-Requested-By: xuanpu``，或 ``Origin`` 与对外地址不符。
    """
    if method.upper() in SAFE_METHODS:
        return
    if headers.get(REQUESTED_BY_HEADER) != REQUESTED_BY_VALUE:
        raise CsrfRejectedError(
            f"用登录 cookie 发起的写请求必须带 {REQUESTED_BY_HEADER}: {REQUESTED_BY_VALUE} 头"
        )
    origin = headers.get("Origin")
    if origin is not None and public_origin is not None and origin != public_origin:
        # 浏览器发出的 Origin 是规整过的（小写、省略默认端口），与规整后的对外地址逐字比较；
        # 不透明来源发的是字面量 "null"，同样不符
        raise CsrfRejectedError("请求来源与平台对外地址（public_url）不符")


__all__ = [
    "LOGIN_COOKIE",
    "REQUESTED_BY_HEADER",
    "REQUESTED_BY_VALUE",
    "SAFE_METHODS",
    "check_cookie_request",
    "clear_login_cookie",
    "login_token_hash",
    "new_login_token",
    "set_login_cookie",
]
