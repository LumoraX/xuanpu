"""飞书登录用到的几个地址：回调地址、事件地址、``return_to`` 的校验，以及落地页加上失败原因。

都是纯函数。回调地址与事件地址由 ``public_url`` 拼出（平台挂在根路径下，不支持反向代理的子
路径，见运维手册）。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit, urlunsplit

from xuanpu_server.identity.config import SITE_PATH

if TYPE_CHECKING:
    from xuanpu_server.identity.models import LoginFailureReason

CALLBACK_PATH = "/v1/auth/feishu/callback"
"""飞书登录的回调路径（要填进飞书后台的重定向地址就是 ``<public_url>`` 加上它）。"""
EVENTS_PATH_PREFIX = "/v1/identity/feishu/events/"
"""飞书事件回调的路径前缀，后面跟租户标识。"""
MAX_RETURN_TO_LENGTH = 512
"""``return_to`` 的最大长度；更长的按不合格处理（它要存进登录尝试，未认证的请求就能写）。"""
_SITE_PATH = re.compile(SITE_PATH)


def callback_url(public_url: str) -> str:
    """飞书登录的回调地址（要填进飞书后台的重定向地址）。"""
    return public_url + CALLBACK_PATH


def event_url(public_url: str, tenant_id: str) -> str:
    """租户的飞书事件地址（要填进飞书后台的事件订阅请求地址）。"""
    return f"{public_url}{EVENTS_PATH_PREFIX}{tenant_id}"


def safe_return_to(value: str | None, *, fallback: str) -> str:
    """登录完成后跳去哪：只收站内路径，否则用 ``fallback``（落地页）。

    站内路径：以 ``/`` 开头、不以 ``//`` 或 ``/\\`` 开头，不含反斜杠、空白与控制字符，不超过
    ``MAX_RETURN_TO_LENGTH``。``https://evil.com``、``//evil.com``、``/\\evil.com`` 这类会被浏览器
    当成别的站点的，一律不收，防止被利用做开放跳转。
    """
    if value is None or len(value) > MAX_RETURN_TO_LENGTH:
        return fallback
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return fallback
    return value if _SITE_PATH.match(value) else fallback


def with_login_error(path: str, reason: LoginFailureReason) -> str:
    """落地页加上 ``login_error=<原因>``（保留路径里已有的查询串，放在片段之前）。"""
    parts = urlsplit(path)
    query = "&".join(item for item in (parts.query, urlencode({"login_error": reason})) if item)
    return urlunsplit(("", "", parts.path, query, parts.fragment))


__all__ = [
    "CALLBACK_PATH",
    "EVENTS_PATH_PREFIX",
    "MAX_RETURN_TO_LENGTH",
    "callback_url",
    "event_url",
    "safe_return_to",
    "with_login_error",
]
