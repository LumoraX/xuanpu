"""登录与登录尝试的 cookie，以及 cookie 认证请求的防跨站请求伪造检查（设计 6.2，另有加固）。

**cookie**：值是 32 字节随机数（URL 安全的 base64）；HttpOnly、SameSite=Lax、Path=/，不带
Domain。平台只存令牌的 SHA-256 摘要，令牌本身不落库。名字随 ``identity.cookie_secure``：

- 为真（默认）：``__Host-xuanpu_session``，并带 Secure。``__Host-`` 前缀要求 Secure、Path=/、
  不带 Domain，浏览器因此拒收兄弟子域或父域写来的同名 cookie；
- 为假（本地用 HTTP 开发）：``xuanpu_session``，不带 Secure。

读 cookie 时只认当前配置的那个名字。同名的 cookie 出现不止一次（可能是别的子域往父域塞了一个），
直接按凭据无效处理，**不清 cookie**：清掉会让塞 cookie 的人能持续把员工踢下线；也不随便挑一个
用（Starlette 默认取最后一个），免得会话被调包。

**没有配置 ``public_url`` 时平台不认登录 cookie**（带 cookie 的请求按没有凭据处理，见
``api.auth``）：飞书登录要求 ``public_url``，没有它就不会签发 cookie；这样下面两条检查在认
cookie 时总是都生效。

**登录尝试 cookie**（对设计的加固，防登录伪造）：开始飞书登录时另写一个 cookie，值是独立的
32 字节随机数，登录尝试里只存它的摘要；回调时浏览器出示的必须与摘要对上，回调的响应总把它清掉。
没有它，别人可以把自己那次登录的回调链接发给员工，员工点开就以别人的身份登录了。名字同样随
``identity.cookie_secure``：``__Host-xuanpu_login``（带 Secure）或 ``xuanpu_login``；HttpOnly、
SameSite=Lax、Path=/，有效期与登录尝试相同。SameSite=Lax 的 cookie 在飞书授权页跳回平台（顶层
GET 导航）时照样会带上。

**防跨站请求伪造**：用 cookie 认证的写请求（GET、HEAD、OPTIONS 以外的方法）必须同时满足：

1. 带 ``X-Requested-By: xuanpu`` 头。平台不开放带凭据的跨域（不发 CORS 头），跨站页面的
   脚本发不出带自定义头的请求，表单与导航又带不了自定义头——这一条本身就挡住了跨站请求伪造；
2. 请求带了 ``Origin`` 的，它必须等于 ``public_url`` 的源。这是第二道防线。

不满足的返回 ``403 csrf_rejected``。这些检查只看请求的形状、不查库，在验会话之前做：伪造的请求
不必走到查库那一步（所以带着失效 cookie、又缺自定义头的写请求得到的是 403 而不是 401）。用
API key（``Authorization`` 头）认证的请求不受这些限制：浏览器不会自动带上 ``Authorization``。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from xuanpu_server.errors import AuthenticationError, CsrfRejectedError
from xuanpu_server.tenancy import hash_secret

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from starlette.responses import Response

LOGIN_COOKIE = "xuanpu_session"
"""登录 cookie 的名字（``cookie_secure`` 为假时）。"""
SECURE_LOGIN_COOKIE = "__Host-xuanpu_session"
"""登录 cookie 的名字（``cookie_secure`` 为真时）。"""
LOGIN_ATTEMPT_COOKIE = "xuanpu_login"
"""登录尝试 cookie 的名字（``cookie_secure`` 为假时）。"""
SECURE_LOGIN_ATTEMPT_COOKIE = "__Host-xuanpu_login"
"""登录尝试 cookie 的名字（``cookie_secure`` 为真时）。"""
REQUESTED_BY_HEADER = "X-Requested-By"
"""cookie 认证的写请求必须带的头。"""
REQUESTED_BY_VALUE = "xuanpu"
"""``X-Requested-By`` 头必须等于的值。"""
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
"""不改状态的方法：cookie 认证时不做防跨站请求伪造的检查。"""


def login_cookie_name(secure: bool) -> str:
    """登录 cookie 的名字：只走 HTTPS 时带 ``__Host-`` 前缀。"""
    return SECURE_LOGIN_COOKIE if secure else LOGIN_COOKIE


def login_attempt_cookie_name(secure: bool) -> str:
    """登录尝试 cookie 的名字：只走 HTTPS 时带 ``__Host-`` 前缀。"""
    return SECURE_LOGIN_ATTEMPT_COOKIE if secure else LOGIN_ATTEMPT_COOKIE


def new_login_token() -> tuple[str, str]:
    """签发一个登录令牌，返回 ``(令牌, 摘要)``。令牌只写进 cookie，库里只存摘要。"""
    token = secrets.token_urlsafe(32)
    return token, login_token_hash(token)


def login_token_hash(token: str) -> str:
    """登录令牌的摘要（SHA-256 十六进制）。令牌是高熵随机串，不需要慢哈希。"""
    return hash_secret(token)


def _set(response: Response, name: str, value: str, *, max_age: int, secure: bool) -> None:
    """按模块说明的属性写一个 cookie。"""
    response.set_cookie(
        name, value, max_age=max_age, path="/", secure=secure, httponly=True, samesite="lax"
    )


def _clear(response: Response, name: str, *, secure: bool) -> None:
    """清掉一个 cookie。名字与属性要与写入时相同，浏览器才认作同一个 cookie。"""
    response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="lax")


def set_login_cookie(response: Response, token: str, *, max_age: int, secure: bool) -> None:
    """把登录令牌写进 cookie。名字与属性见模块说明；``max_age`` 与登录会话的有效期一致。"""
    _set(response, login_cookie_name(secure), token, max_age=max_age, secure=secure)


def clear_login_cookie(response: Response, *, secure: bool) -> None:
    """清掉浏览器里的登录 cookie。"""
    _clear(response, login_cookie_name(secure), secure=secure)


def set_login_attempt_cookie(response: Response, value: str, *, max_age: int, secure: bool) -> None:
    """写登录尝试 cookie。``max_age`` 与登录尝试的有效期一致。"""
    _set(response, login_attempt_cookie_name(secure), value, max_age=max_age, secure=secure)


def clear_login_attempt_cookie(response: Response, *, secure: bool) -> None:
    """清掉登录尝试 cookie（回调之后它就没用了）。"""
    _clear(response, login_attempt_cookie_name(secure), secure=secure)


def cookie_values(cookie_headers: Iterable[str], *, name: str) -> list[str]:
    """从请求的 ``Cookie`` 头（可能不止一个）里取某个名字的全部取值，按出现顺序。

    只认名字完全相同的（cookie 名区分大小写）；取值原样返回，不去引号（平台写的值不带引号）。
    """
    return [
        value.strip()
        for header in cookie_headers
        for key, sep, value in (chunk.partition("=") for chunk in header.split(";"))
        if sep and key.strip() == name
    ]


def read_login_cookie(cookie_headers: Iterable[str], *, name: str) -> str | None:
    """从请求的 ``Cookie`` 头里取登录令牌；没有这个 cookie 返回 None。

    Raises:
        AuthenticationError: 同名 cookie 出现不止一次（见模块说明；调用方不要因此清 cookie）。
    """
    values = cookie_values(cookie_headers, name=name)
    if len(values) > 1:
        raise AuthenticationError("凭据无效")
    return values[0] if values else None


def check_cookie_request(method: str, headers: Mapping[str, str], *, public_origin: str) -> None:
    """对一个用 cookie 认证的请求做防跨站请求伪造的检查（规则见模块说明）。

    Args:
        method: 请求方法。
        headers: 请求头（键不区分大小写的映射，如 Starlette 的 ``Headers``）。
        public_origin: 平台对外地址的源（``public_url``，已规整为 ``scheme://host[:port]``）。

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
    if origin is not None and origin != public_origin:
        # 浏览器发出的 Origin 是规整过的（小写、省略默认端口），与规整后的对外地址逐字比较；
        # 不透明来源发的是字面量 "null"，同样不符
        raise CsrfRejectedError("请求来源与平台对外地址（public_url）不符")


__all__ = [
    "LOGIN_ATTEMPT_COOKIE",
    "LOGIN_COOKIE",
    "REQUESTED_BY_HEADER",
    "REQUESTED_BY_VALUE",
    "SAFE_METHODS",
    "SECURE_LOGIN_ATTEMPT_COOKIE",
    "SECURE_LOGIN_COOKIE",
    "check_cookie_request",
    "clear_login_attempt_cookie",
    "clear_login_cookie",
    "cookie_values",
    "login_attempt_cookie_name",
    "login_cookie_name",
    "login_token_hash",
    "new_login_token",
    "read_login_cookie",
    "set_login_attempt_cookie",
    "set_login_cookie",
]
