"""飞书客户端：拼授权地址、PKCE、换令牌、取用户信息、应用身份令牌、批量查员工，以及两层错误。

模拟服务端用 ``httpx.MockTransport``。失败用例都断言：异常文本、``repr`` 与日志里没有授权码、
令牌、App Secret。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from xuanpu_server.identity.feishu import (
    MAX_BATCH_USERS,
    AccessToken,
    FeishuClient,
    FeishuError,
    code_challenge,
    new_code_verifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable

OPEN = "https://open.feishu.test"
ACCOUNTS = "https://accounts.feishu.test"
APP_ID = "cli_a1b2c3d4"
SECRET = "app-secret-SENSITIVE-1"
CODE = "auth-code-SENSITIVE-2"
TOKEN = "u-token-SENSITIVE-3"
TENANT_TOKEN = "t-token-SENSITIVE-4"
VERIFIER = "verifier-SENSITIVE-5-" + "v" * 30
REDIRECT = "https://xuanpu.example.com/v1/auth/feishu/callback"
SENSITIVE = (SECRET, CODE, TOKEN, TENANT_TOKEN, VERIFIER)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> FeishuClient:
    """接到模拟服务端的客户端。"""
    return FeishuClient(OPEN, ACCOUNTS, transport=httpx.MockTransport(handler), timeout_seconds=5.0)


def _json(status: int, body: Any) -> httpx.Response:
    """一个 JSON 响应。"""
    return httpx.Response(status, json=body)


def _assert_clean(*texts: str) -> None:
    """这些文本里没有任何授权码、令牌、App Secret 与 PKCE 校验串。"""
    for text in texts:
        for secret in SENSITIVE:
            assert secret not in text


async def _exchange(client: FeishuClient) -> AccessToken:
    """用固定参数换令牌。"""
    return await client.exchange_code(
        app_id=APP_ID, app_secret=SECRET, code=CODE, redirect_uri=REDIRECT, code_verifier=VERIFIER
    )


# --- 授权地址与 PKCE ---


def test_authorize_url_carries_pkce_and_no_scope() -> None:
    """授权地址：飞书授权页的路径，参数是 client_id、response_type、redirect_uri、state、
    code_challenge 与 S256；不申请额外权限（没有 scope）。"""
    client = FeishuClient(OPEN, ACCOUNTS, timeout_seconds=5.0)
    url = client.authorize_url(
        app_id=APP_ID, redirect_uri=REDIRECT, state="st-1", code_challenge="ch-1"
    )
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}" == ACCOUNTS
    assert parts.path == "/open-apis/authen/v1/authorize"
    assert parse_qs(parts.query, strict_parsing=True) == {
        "client_id": [APP_ID],
        "response_type": ["code"],
        "redirect_uri": [REDIRECT],
        "state": ["st-1"],
        "code_challenge": ["ch-1"],
        "code_challenge_method": ["S256"],
    }


def test_code_challenge_follows_rfc7636() -> None:
    """S256：challenge 是校验串 SHA-256 的 base64url（不带填充）。用 RFC 7636 附录 B 的样例核对。"""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_new_code_verifier_shape() -> None:
    """校验串 43–128 个字符，只含 RFC 7636 允许的字符；每次不同。"""
    first, second = new_code_verifier(), new_code_verifier()
    assert first != second
    assert re.fullmatch(r"[A-Za-z0-9\-._~]{43,128}", first)
    digest = hashlib.sha256(first.encode("ascii")).digest()
    assert code_challenge(first) == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# --- 成功 ---


async def test_exchange_code_posts_json_body() -> None:
    """换令牌：POST JSON 体（授权码模式的六个字段），令牌在响应顶层；令牌不进 repr。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记下请求，返回令牌。"""
        seen.append(request)
        body = {"code": 0, "access_token": TOKEN, "expires_in": 7200, "token_type": "Bearer"}
        return _json(200, body)

    token = await _exchange(_client(handler))
    assert (token.value, token.expires_in) == (TOKEN, 7200)
    _assert_clean(repr(token), str(token))
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", f"{OPEN}/open-apis/authen/v2/oauth/token")
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "grant_type": "authorization_code",
        "client_id": APP_ID,
        "client_secret": SECRET,
        "code": CODE,
        "redirect_uri": REDIRECT,
        "code_verifier": VERIFIER,
    }


async def test_user_info_uses_the_user_token() -> None:
    """取用户信息：带员工令牌，字段在 data 里；空头像按没有处理。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记下请求，返回用户信息。"""
        seen.append(request)
        data = {
            "name": "张三",
            "avatar_url": "",
            "open_id": "ou_1",
            "union_id": "on_1",
            "tenant_key": "tk_acme",
        }
        return _json(200, {"code": 0, "msg": "success", "data": data})

    info = await _client(handler).user_info(AccessToken(TOKEN, 7200))
    assert (info.name, info.avatar_url) == ("张三", None)
    assert (info.open_id, info.union_id, info.tenant_key) == ("ou_1", "on_1", "tk_acme")
    (request,) = seen
    assert (request.method, str(request.url)) == ("GET", f"{OPEN}/open-apis/authen/v1/user_info")
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


async def test_tenant_access_token() -> None:
    """应用身份令牌：POST JSON 体 app_id、app_secret；令牌与有效期在响应顶层（tenant_access_token、
    expire）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记下请求，返回应用身份令牌。"""
        seen.append(request)
        body = {"code": 0, "msg": "ok", "tenant_access_token": TENANT_TOKEN, "expire": 6000}
        return _json(200, body)

    token = await _client(handler).tenant_access_token(app_id=APP_ID, app_secret=SECRET)
    assert (token.value, token.expires_in) == (TENANT_TOKEN, 6000)
    (request,) = seen
    assert str(request.url) == f"{OPEN}/open-apis/auth/v3/tenant_access_token/internal"
    assert request.method == "POST"
    assert json.loads(request.content) == {"app_id": APP_ID, "app_secret": SECRET}


async def test_batch_users_by_union_id() -> None:
    """批量查员工：user_ids 逐个作查询参数，user_id_type=union_id，带应用身份令牌；状态五项原样
    带回，没给的为 None。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记下请求，返回两名员工。"""
        seen.append(request)
        items = [
            {"union_id": "on_1", "open_id": "ou_1", "status": {"is_resigned": True}},
            {
                "union_id": "on_2",
                "status": {
                    "is_frozen": False,
                    "is_resigned": False,
                    "is_activated": True,
                    "is_exited": False,
                    "is_unjoin": False,
                },
            },
        ]
        return _json(200, {"code": 0, "data": {"items": items}})

    users = await _client(handler).batch_users(AccessToken(TENANT_TOKEN, 6000), ["on_1", "on_2"])
    assert [user.union_id for user in users] == ["on_1", "on_2"]
    assert users[0].is_resigned is True
    assert users[0].is_frozen is None
    assert users[1].is_activated is True
    assert users[1].is_exited is False
    (request,) = seen
    assert request.url.path == "/open-apis/contact/v3/users/batch"
    assert request.url.params.get_list("user_ids") == ["on_1", "on_2"]
    assert request.url.params["user_id_type"] == "union_id"
    assert request.headers["authorization"] == f"Bearer {TENANT_TOKEN}"


async def test_batch_users_limits() -> None:
    """一批最多 50 人，超出报 ValueError、不发请求；空列表直接返回空、不发请求；没查到人时
    响应里没有 items 也是空。"""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """记下请求，返回空结果。"""
        calls.append(request)
        return _json(200, {"code": 0, "data": {}})

    client = _client(handler)
    token = AccessToken(TENANT_TOKEN, 6000)
    with pytest.raises(ValueError, match="50"):
        await client.batch_users(token, [f"on_{index}" for index in range(MAX_BATCH_USERS + 1)])
    assert await client.batch_users(token, []) == []
    assert calls == []
    assert await client.batch_users(token, ["on_x"]) == []
    assert len(calls) == 1


async def test_batch_users_rejects_malformed_status() -> None:
    """状态里给了却不是布尔值的：按响应不合预期（unavailable）报错，不当成「没给」吞掉。"""

    def handler(_: httpx.Request) -> httpx.Response:
        """is_resigned 是字符串。"""
        items = [{"union_id": "on_1", "status": {"is_resigned": "true"}}]
        return _json(200, {"code": 0, "data": {"items": items}})

    with pytest.raises(FeishuError) as caught:
        await _client(handler).batch_users(AccessToken(TENANT_TOKEN, 6000), ["on_1"])
    assert caught.value.kind == "unavailable"


# --- 失败：两层错误 ---


def _raise_network(_: httpx.Request) -> httpx.Response:
    """连不上飞书。"""
    raise httpx.ConnectError("connection refused")


ERROR_CASES: list[tuple[str, Callable[[httpx.Request], httpx.Response], str, int | None]] = [
    (
        "not_in_app_scope",
        lambda _: _json(400, {"code": 20010, "error": "unauthorized_client"}),
        "not_in_app_scope",
        20010,
    ),
    (
        "token_error_code",
        lambda _: _json(400, {"code": 20003, "error": "invalid_grant", "error_description": CODE}),
        "denied",
        20003,
    ),
    ("code_in_body_with_200", lambda _: _json(200, {"code": 20004, "msg": CODE}), "denied", 20004),
    ("server_error", lambda _: _json(500, {"code": 1, "msg": "internal"}), "unavailable", 1),
    ("bad_gateway_html", lambda _: httpx.Response(502, text="<html>"), "unavailable", None),
    ("rate_limited", lambda _: _json(429, {"code": 99991400}), "unavailable", 99991400),
    ("not_json", lambda _: httpx.Response(200, text="<html>ok</html>"), "unavailable", None),
    ("json_not_object", lambda _: _json(200, ["code", 0]), "unavailable", None),
    ("missing_token", lambda _: _json(200, {"code": 0, "expires_in": 7200}), "unavailable", 0),
    ("redirect", lambda _: httpx.Response(302, headers={"Location": "/x"}), "unavailable", None),
    ("network", _raise_network, "unavailable", None),
]


@pytest.mark.parametrize(
    ("handler", "kind", "code"),
    [case[1:] for case in ERROR_CASES],
    ids=[case[0] for case in ERROR_CASES],
)
async def test_exchange_code_errors(
    handler: Callable[[httpx.Request], httpx.Response],
    kind: str,
    code: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """换令牌的各种失败归成三类；异常文本、repr 与日志里都没有授权码、令牌、Secret、校验串
    （包括飞书在错误描述里原样回显的内容）。"""
    caplog.set_level(logging.DEBUG)
    with pytest.raises(FeishuError) as caught:
        await _exchange(_client(handler))
    error = caught.value
    assert (error.kind, error.code) == (kind, code)
    assert error.operation == "换令牌"
    _assert_clean(str(error), repr(error), caplog.text)


async def test_error_carries_status_and_safe_error_type() -> None:
    """异常带 HTTP 状态与飞书错误码；飞书给的错误类型（形如 invalid_grant）写进文本，错误描述
    不写（可能回显请求内容）。"""

    def handler(_: httpx.Request) -> httpx.Response:
        """授权码无效。"""
        body = {"code": 20003, "error": "invalid_grant", "error_description": "bad " + CODE}
        return _json(400, body)

    with pytest.raises(FeishuError) as caught:
        await _exchange(_client(handler))
    assert caught.value.status == 400
    assert "invalid_grant" in str(caught.value)
    assert "20003" in str(caught.value)
    _assert_clean(str(caught.value))


async def test_user_info_errors() -> None:
    """取用户信息：令牌无效（飞书错误码）是 denied；data 缺字段是 unavailable。"""

    def invalid_token(_: httpx.Request) -> httpx.Response:
        """令牌无效。"""
        return _json(401, {"code": 20005, "msg": "invalid access token " + TOKEN})

    def missing_union_id(_: httpx.Request) -> httpx.Response:
        """缺 union_id。"""
        data = {"name": "张三", "open_id": "ou_1", "tenant_key": "tk"}
        return _json(200, {"code": 0, "data": data})

    for handler, kind in ((invalid_token, "denied"), (missing_union_id, "unavailable")):
        with pytest.raises(FeishuError) as caught:
            await _client(handler).user_info(AccessToken(TOKEN, 7200))
        assert caught.value.kind == kind
        assert caught.value.operation == "取用户信息"
        _assert_clean(str(caught.value))


async def test_tenant_access_token_error_hides_the_secret(caplog: pytest.LogCaptureFixture) -> None:
    """应用身份令牌：App Secret 不对是 denied，异常与日志里没有 Secret。"""
    caplog.set_level(logging.DEBUG)

    def handler(_: httpx.Request) -> httpx.Response:
        """应用凭据不对。"""
        return _json(200, {"code": 10014, "msg": "app secret invalid"})

    with pytest.raises(FeishuError) as caught:
        await _client(handler).tenant_access_token(app_id=APP_ID, app_secret=SECRET)
    assert (caught.value.kind, caught.value.code) == ("denied", 10014)
    _assert_clean(str(caught.value), repr(caught.value), caplog.text)


async def test_client_closes() -> None:
    """关闭可以重复调用。"""
    client = _client(lambda _: _json(200, {"code": 0}))
    await client.aclose()
    await client.aclose()
