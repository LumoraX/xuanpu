"""飞书开放接口的最小客户端：拼授权地址、换令牌、取用户信息、取应用身份令牌、批量查员工。

不引入飞书 SDK 与 OAuth 库（ADR 0010、设计第 13 节）：剩下的就是这五件事，用 ``httpx`` 与标准库
写完。两个基地址（开放接口、授权页）与 httpx 传输层都由构造参数注入：测试时换成模拟服务端，以后
也能接 Lark 国际版。

**接口与核对出处**（设计第 3 节已核对授权页、换令牌、取用户信息、批量查员工；下面三项是 2026-10-02
实现时对照飞书开放平台文档补核的）：

- 应用身份令牌 ``POST /open-apis/auth/v3/tenant_access_token/internal``：请求体 JSON
  ``app_id``、``app_secret``；响应顶层 ``code``、``msg``、``tenant_access_token``、``expire``
  （秒，最长 2 小时；剩余不足 30 分钟时飞书才发新的）。已核对：飞书开放平台「自建应用获取
  tenant_access_token」文档。
- 换令牌 ``POST /open-apis/authen/v2/oauth/token``：令牌与错误都在响应顶层（成功
  ``access_token``、``expires_in``；失败 ``code``、``error``、``error_description``），HTTP 400
  加 ``code`` 20010 是「用户无应用使用权限」，20003/20004/20049 是授权码无效、过期、PKCE 不符。
  已核对：飞书开放平台「获取 user_access_token（v2）」文档。
- 批量查员工的 ``user_ids`` 逐个作查询参数（``?user_ids=a&user_ids=b``）：已核对官方 Python SDK
  ``lark-oapi`` 的 ``BatchUserRequest`` 与 ``BaseRequest.add_query``（列表逐个展开）。

设计第 3 节待核对的另两项属于事件回调（不在本客户端里，记在这里供实现事件回调时用）：URL 验证
请求未加密时是 ``{"challenge", "token", "type": "url_verification"}``，配了 Encrypt Key 时整体在
``encrypt`` 字段里，要在 1 秒内原样回 ``{"challenge": …}``；事件推送要在 3 秒内回 HTTP 200，否则
按 15 秒、5 分钟、1 小时、6 小时的间隔重推，最多 4 次。已核对：飞书开放平台「事件概述」与「将事件
发送至开发者服务器」文档。

**错误分两层**：HTTP 状态码，加响应体里的 ``code``（0 才算成功）。统一报成 ``FeishuError``，按
``kind`` 分三类：

| ``kind`` | 何时 |
| --- | --- |
| ``not_in_app_scope`` | 响应体 ``code`` 为 20010：员工不在自建应用的可用范围内 |
| ``denied`` | 飞书明确拒绝：HTTP 4xx（429 除外），或 HTTP 200 而 ``code`` 不为 0 |
| ``unavailable`` | 网络错误、超时、HTTP 5xx 或 429、重定向；响应体过大、不是 JSON 对象或缺字段 |

**授权码、令牌、App Secret、PKCE 校验串不进日志、不进异常文本、不进 repr**：异常文本只有接口名、
分类、HTTP 状态、飞书的错误码，以及形如 ``invalid_grant`` 的错误类型；飞书给的错误描述
（``msg``、``error_description``）一概不用——它们可能原样回显请求里的内容。令牌只经
``AccessToken.value`` 取用，``repr`` 里没有它。本模块不写日志，失败由调用方按需记录。

**时限与大小**：每次请求（连接、发送、读完响应体）有一个总时限 ``timeout_seconds``，不只是 httpx
那种按单个环节计的超时——对端一点一点地吐数据也拖不过它；响应体边读边数，超过 1 MiB 就不再读。

**出口**：只连 ``open_base``（默认 ``open.feishu.cn``），不读环境里的代理设置；``accounts_base``
只用来拼授权页地址，是员工的浏览器去访问。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

import httpx

if TYPE_CHECKING:
    from collections.abc import Sequence

FeishuErrorKind = Literal["not_in_app_scope", "denied", "unavailable"]

NOT_IN_APP_SCOPE = 20010
"""飞书错误码：用户不在自建应用的可用范围内（用户无应用使用权限）。"""
MAX_BATCH_USERS = 50
"""批量查员工一次最多查这么多人。"""
DEFAULT_TIMEOUT_SECONDS = 10.0
"""平台装配客户端时用的单次请求的总时限（秒）。"""
MAX_RESPONSE_BYTES = 1024 * 1024
"""响应体的上限：飞书这几个接口的正常响应只有几 KB，超过 1 MiB 按响应不合预期处理。"""

_AUTHORIZE_PATH = "/open-apis/authen/v1/authorize"
_TOKEN_PATH = "/open-apis/authen/v2/oauth/token"  # noqa: S105 —— 接口路径，不是凭据
_USER_INFO_PATH = "/open-apis/authen/v1/user_info"
_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"  # noqa: S105 —— 同上
_BATCH_USERS_PATH = "/open-apis/contact/v3/users/batch"
# 飞书的错误类型（如 invalid_grant）是枚举值，可以写进异常文本；别的形状一概不写
_ERROR_TYPE = re.compile(r"^[a-z_]{1,64}$")
_STATUS_FLAGS = ("is_frozen", "is_resigned", "is_activated", "is_exited", "is_unjoin")


class FeishuError(Exception):
    """调飞书接口失败。分类见模块说明。

    Attributes:
        kind: ``not_in_app_scope``、``denied`` 或 ``unavailable``。
        operation: 哪个接口（中文名，如「换令牌」）。
        status: HTTP 状态码；没有收到响应（网络错误）时为 None。
        code: 响应体里飞书的错误码；响应体不是 JSON 对象时为 None。
    """

    def __init__(
        self,
        kind: FeishuErrorKind,
        operation: str,
        *,
        status: int | None = None,
        code: int | None = None,
        detail: str = "",
    ) -> None:
        """组织异常文本：只有接口名、分类、状态码、错误码与 ``detail``（调用方保证不含机密）。"""
        parts = [f"HTTP {status}" if status is not None else "没有收到响应"]
        if code is not None:
            parts.append(f"code {code}")
        if detail:
            parts.append(detail)
        super().__init__(f"飞书接口「{operation}」失败（{kind}）：{'，'.join(parts)}")
        self.kind: FeishuErrorKind = kind
        self.operation = operation
        self.status = status
        self.code = code


@dataclass(frozen=True)
class AccessToken:
    """飞书的访问令牌（员工令牌或应用身份令牌）。

    Attributes:
        value: 令牌本身；不进 repr。
        expires_in: 有效期（秒），从签发时算起。
    """

    value: str = field(repr=False)
    expires_in: int


@dataclass(frozen=True)
class FeishuUserInfo:
    """取用户信息接口返回的、平台用得上的字段（不需要额外权限就能拿到）。

    Attributes:
        union_id: 同一开发商（自建应用即本企业）各应用间不变的标识；平台按它认人。
        open_id: 本应用内的标识，应用重建后会变；存进外部身份的属性，供以后调飞书接口用。
        tenant_key: 飞书企业标识；首登绑定到租户，之后必须一致。
        name: 姓名。
        avatar_url: 头像地址；飞书没给或给了空串为 None。
    """

    union_id: str
    open_id: str
    tenant_key: str
    name: str
    avatar_url: str | None


@dataclass(frozen=True)
class FeishuContactUser:
    """批量查员工返回的一名员工。状态的五项原样带回，飞书没给的为 None（由调用方决定怎么处理）。

    Attributes:
        union_id: 员工的 ``union_id``。
        is_frozen: 是否被暂停。
        is_resigned: 是否已离职。
        is_activated: 是否已激活。
        is_exited: 是否主动退出。
        is_unjoin: 是否未加入。
    """

    union_id: str
    is_frozen: bool | None
    is_resigned: bool | None
    is_activated: bool | None
    is_exited: bool | None
    is_unjoin: bool | None


def new_code_verifier() -> str:
    """新的 PKCE 校验串：48 字节随机数的 base64url，64 个字符（RFC 7636 要求 43–128 个）。"""
    return secrets.token_urlsafe(48)


def code_challenge(verifier: str) -> str:
    """PKCE 的 S256 challenge：校验串 SHA-256 摘要的 base64url，不带填充（RFC 7636 4.2）。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class FeishuClient:
    """飞书开放接口的客户端。一个平台一个，进程内复用连接；用完调 ``aclose``。"""

    def __init__(
        self,
        open_base: str,
        accounts_base: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float,
    ) -> None:
        """
        Args:
            open_base: 开放接口的基地址（只写源，如 ``https://open.feishu.cn``）。
            accounts_base: 授权页的基地址（如 ``https://accounts.feishu.cn``）。
            transport: 自定义 httpx 传输；None 用默认的。测试时换成模拟服务端。
            timeout_seconds: 单次请求的总时限：连接、发送、读完响应体一共这么久。
        """
        self._open_base = open_base.rstrip("/")
        self._accounts_base = accounts_base.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            transport=transport,
            # 不读环境里的代理与证书配置：出口走哪里由部署决定，不由进程环境隐式决定
            trust_env=False,
        )

    def authorize_url(
        self, *, app_id: str, redirect_uri: str, state: str, code_challenge: str
    ) -> str:
        """员工要跳去的飞书授权页地址。PKCE 用 S256；不带 ``scope``，不申请额外权限。"""
        query = urlencode(
            {
                "client_id": app_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._accounts_base}{_AUTHORIZE_PATH}?{query}"

    async def exchange_code(
        self, *, app_id: str, app_secret: str, code: str, redirect_uri: str, code_verifier: str
    ) -> AccessToken:
        """用授权码与 PKCE 校验串换员工令牌。

        Raises:
            FeishuError: 见模块说明；员工不在可用范围内是 ``not_in_app_scope``。
        """
        operation = "换令牌"
        body = await self._call(
            operation,
            "POST",
            _TOKEN_PATH,
            json_body={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return AccessToken(
            _require_str(body, "access_token", operation),
            _require_int(body, "expires_in", operation),
        )

    async def user_info(self, token: AccessToken) -> FeishuUserInfo:
        """用员工令牌取他的身份信息。

        Raises:
            FeishuError: 见模块说明；``data`` 缺必需字段是 ``unavailable``。
        """
        operation = "取用户信息"
        body = await self._call(operation, "GET", _USER_INFO_PATH, token=token)
        data = _require_object(body, "data", operation)
        avatar = data.get("avatar_url")
        return FeishuUserInfo(
            union_id=_require_str(data, "union_id", operation),
            open_id=_require_str(data, "open_id", operation),
            tenant_key=_require_str(data, "tenant_key", operation),
            name=_require_str(data, "name", operation),
            avatar_url=avatar if isinstance(avatar, str) and avatar else None,
        )

    async def tenant_access_token(self, *, app_id: str, app_secret: str) -> AccessToken:
        """取应用身份令牌（自建应用）。缓存由调用方做。

        Raises:
            FeishuError: 见模块说明；App ID 或 App Secret 不对是 ``denied``。
        """
        operation = "取应用身份令牌"
        body = await self._call(
            operation,
            "POST",
            _TENANT_TOKEN_PATH,
            json_body={"app_id": app_id, "app_secret": app_secret},
        )
        return AccessToken(
            _require_str(body, "tenant_access_token", operation),
            _require_int(body, "expire", operation),
        )

    async def batch_users(
        self, token: AccessToken, union_ids: Sequence[str]
    ) -> list[FeishuContactUser]:
        """按 ``union_id`` 批量查员工（带应用身份令牌）。不在应用通讯录权限范围内的人不会返回。

        Raises:
            ValueError: 一次超过 ``MAX_BATCH_USERS`` 人。
            FeishuError: 见模块说明。
        """
        if len(union_ids) > MAX_BATCH_USERS:
            raise ValueError(f"一次最多查 {MAX_BATCH_USERS} 人，得到 {len(union_ids)} 人")
        if not union_ids:
            return []
        operation = "批量查员工"
        params = httpx.QueryParams(
            [("user_id_type", "union_id"), *(("user_ids", item) for item in union_ids)]
        )
        body = await self._call(operation, "GET", _BATCH_USERS_PATH, token=token, params=params)
        data = _require_object(body, "data", operation)
        items = data.get("items", [])
        if not isinstance(items, list):
            raise FeishuError("unavailable", operation, status=200, detail="data.items 不是列表")
        return [_contact_user(item, operation) for item in items]

    async def aclose(self) -> None:
        """关闭连接。可以重复调用。"""
        await self._client.aclose()

    async def _call(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, str] | None = None,
        params: httpx.QueryParams | None = None,
        token: AccessToken | None = None,
    ) -> dict[str, Any]:
        """发一次请求，过两层错误检查，返回响应体。

        Raises:
            FeishuError: 见模块说明。
        """
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token.value}"
        try:
            async with asyncio.timeout(self._timeout_seconds):
                status, raw = await self._send(
                    operation, method, path, json_body=json_body, params=params, headers=headers
                )
        except TimeoutError as exc:
            raise FeishuError("unavailable", operation, detail="超时") from exc
        except httpx.HTTPError as exc:
            # httpx 的异常文本不含请求体与请求头；这里只报类型，不拼它的文本
            raise FeishuError(
                "unavailable", operation, detail=f"网络错误 {type(exc).__name__}"
            ) from exc
        return _checked(status, raw, operation)

    async def _send(
        self,
        operation: str,
        method: str,
        path: str,
        *,
        json_body: dict[str, str] | None,
        params: httpx.QueryParams | None,
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        """发请求、边读边数响应体，返回 ``(状态码, 响应体)``。

        Raises:
            FeishuError: 响应体超过 ``MAX_RESPONSE_BYTES``。
            httpx.HTTPError: 网络层失败（由 ``_call`` 转成 ``FeishuError``）。
        """
        url = self._open_base + path
        async with self._client.stream(
            method, url, json=json_body, params=params, headers=headers
        ) as response:
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise FeishuError(
                        "unavailable",
                        operation,
                        status=response.status_code,
                        detail="响应体超过 1 MiB",
                    )
            return response.status_code, bytes(raw)


def _checked(status: int, raw: bytes, operation: str) -> dict[str, Any]:
    """两层错误检查（分类见模块说明），通过时返回解析后的响应体。"""
    try:
        body: Any = json.loads(raw)
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    code = code if isinstance(code, int) and not isinstance(code, bool) else None
    error_type = body.get("error") if isinstance(body, dict) else None
    detail = error_type if isinstance(error_type, str) and _ERROR_TYPE.match(error_type) else ""
    if code == NOT_IN_APP_SCOPE:
        raise FeishuError("not_in_app_scope", operation, status=status, code=code, detail=detail)
    if status >= 500 or status == 429:
        raise FeishuError("unavailable", operation, status=status, code=code, detail=detail)
    if 400 <= status < 500:
        raise FeishuError("denied", operation, status=status, code=code, detail=detail)
    if status != 200 or not isinstance(body, dict) or code is None:
        raise FeishuError(
            "unavailable", operation, status=status, code=code, detail="响应不是预期的 JSON 对象"
        )
    if code != 0:
        raise FeishuError("denied", operation, status=status, code=code, detail=detail)
    return body


def _require_object(body: dict[str, Any], key: str, operation: str) -> dict[str, Any]:
    """响应里必须有的 JSON 对象字段。"""
    value = body.get(key)
    if not isinstance(value, dict):
        raise FeishuError("unavailable", operation, status=200, code=0, detail=f"响应缺少 {key}")
    return value


def _require_str(body: dict[str, Any], key: str, operation: str) -> str:
    """响应里必须有的非空字符串字段。"""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise FeishuError("unavailable", operation, status=200, code=0, detail=f"响应缺少 {key}")
    return value


def _require_int(body: dict[str, Any], key: str, operation: str) -> int:
    """响应里必须有的整数字段。"""
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise FeishuError("unavailable", operation, status=200, code=0, detail=f"响应缺少 {key}")
    return value


def _contact_user(item: Any, operation: str) -> FeishuContactUser:
    """批量查员工结果里的一项。状态里没给的项为 None；给了却不是布尔值的，按响应不合预期报错。"""
    if not isinstance(item, dict):
        raise FeishuError("unavailable", operation, status=200, code=0, detail="items 里有非对象")
    status = item.get("status", {})
    if not isinstance(status, dict):
        raise FeishuError("unavailable", operation, status=200, code=0, detail="status 不是对象")
    flags: dict[str, bool | None] = {}
    for flag in _STATUS_FLAGS:
        value = status.get(flag)
        if value is not None and not isinstance(value, bool):
            raise FeishuError(
                "unavailable", operation, status=200, code=0, detail=f"status.{flag} 不是布尔值"
            )
        flags[flag] = value
    return FeishuContactUser(
        union_id=_require_str(item, "union_id", operation),
        is_frozen=flags["is_frozen"],
        is_resigned=flags["is_resigned"],
        is_activated=flags["is_activated"],
        is_exited=flags["is_exited"],
        is_unjoin=flags["is_unjoin"],
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_BATCH_USERS",
    "MAX_RESPONSE_BYTES",
    "NOT_IN_APP_SCOPE",
    "AccessToken",
    "FeishuClient",
    "FeishuContactUser",
    "FeishuError",
    "FeishuErrorKind",
    "FeishuUserInfo",
    "code_challenge",
    "new_code_verifier",
]
