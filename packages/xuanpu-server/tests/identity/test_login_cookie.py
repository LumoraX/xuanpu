"""登录 cookie 的加固：名字随 ``cookie_secure``、同名 cookie 不止一个、没配 ``public_url`` 时不认
cookie、清除时的属性、请求形状先于会话检查、不发 CORS 头，以及 ``/v1/me`` 的内部错误。

夹具 ``identity_world`` 见本目录的 ``conftest.py``。
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from starlette.responses import Response
from xuanpu_server.errors import AuthenticationError
from xuanpu_server.identity.cookies import (
    LOGIN_COOKIE,
    SECURE_LOGIN_COOKIE,
    clear_login_cookie,
    login_cookie_name,
    new_login_token,
    read_login_cookie,
    set_login_cookie,
)

WRITE = {"X-Requested-By": "xuanpu"}
UNAUTHENTICATED = {"error": {"code": "unauthenticated", "message": "凭据无效"}}
LOCAL_URL = "http://127.0.0.1:8420"
"""本地用 HTTP 开发时的对外地址（配 ``identity.cookie_secure: false``）。"""


def _mode(secure: bool) -> dict[str, Any]:
    """``identity_world`` 的参数：只走 HTTPS 的部署，或本地 HTTP 开发。"""
    if secure:
        return {"cookie_secure": True}
    return {"cookie_secure": False, "public_url": LOCAL_URL}


# --- cookie 的名字与属性 ---


@pytest.mark.parametrize("secure", [True, False])
async def test_cookie_name_follows_cookie_secure(identity_world: Any, secure: bool) -> None:
    """只走 HTTPS 时认 ``__Host-xuanpu_session``，本地 HTTP 开发时认 ``xuanpu_session``；另一个名字
    按没有凭据处理（401，不清 cookie）。"""
    async with identity_world(**_mode(secure)) as ident:
        token = await ident.login(await ident.user())
        assert ident.cookie_name == (SECURE_LOGIN_COOKIE if secure else LOGIN_COOKIE)
        assert (await ident.http.get("/v1/me", headers=ident.cookie(token))).status_code == 200
        other = LOGIN_COOKIE if secure else SECURE_LOGIN_COOKIE
        response = await ident.http.get("/v1/me", headers={"Cookie": f"{other}={token}"})
        assert response.status_code == 401
        assert "set-cookie" not in response.headers


@pytest.mark.parametrize("secure", [True, False])
def test_cookie_is_written_and_cleared_alike(secure: bool) -> None:
    """写入与清除用同一个名字、同样的属性：Path=/、HttpOnly、SameSite=Lax，Secure 随配置，不带
    Domain（``__Host-`` 前缀要求 Secure、Path=/、不带 Domain）。"""
    response = Response()
    set_login_cookie(response, "tok", max_age=600, secure=secure)
    clear_login_cookie(response, secure=secure)
    written, cleared = response.headers.getlist("set-cookie")
    name = login_cookie_name(secure)
    assert written.startswith(f"{name}=tok;")
    assert cleared.startswith(f'{name}="";')

    def attributes(header: str) -> set[str]:
        """除了取值、有效期之外的属性。"""
        parts = {part.strip().lower() for part in header.split(";")[1:]}
        return {part for part in parts if not part.startswith(("max-age=", "expires="))}

    expected = {"path=/", "httponly", "samesite=lax"} | ({"secure"} if secure else set())
    assert attributes(written) == attributes(cleared) == expected
    assert "Max-Age=600" in written
    assert "Max-Age=0" in cleared


@pytest.mark.parametrize("secure", [True, False])
async def test_invalid_cookie_is_cleared_with_the_written_attributes(
    identity_world: Any, secure: bool
) -> None:
    """会话失效时清 cookie 的 ``Set-Cookie`` 与写入时的名字、属性一致：本地 HTTP 开发时不带
    Secure（带了浏览器就不认这次清除）。"""
    async with identity_world(**_mode(secure)) as ident:
        response = await ident.http.get("/v1/me", headers=ident.cookie(new_login_token()[0]))
        assert response.status_code == 401
        assert ident.cleared(response)
        assert ("Secure" in response.headers["set-cookie"]) == secure


# --- 同名 cookie 不止一个 ---


def test_read_login_cookie() -> None:
    """只认名字完全相同的；同名不止一个就报错，不挑一个用。"""
    assert read_login_cookie([], name="n") is None
    assert read_login_cookie(["a=1; n=tok ; b=2"], name="n") == "tok"
    assert read_login_cookie(["N=tok; xn=tok"], name="n") is None
    with pytest.raises(AuthenticationError, match="凭据无效"):
        read_login_cookie(["n=a; n=b"], name="n")
    with pytest.raises(AuthenticationError, match="凭据无效"):
        read_login_cookie(["n=a", "n=b"], name="n")


@pytest.mark.parametrize("secure", [True, False])
async def test_duplicate_login_cookies_are_rejected_without_clearing(
    identity_world: Any, secure: bool
) -> None:
    """同名 cookie 不止一个（有效的在前或在后、同一个头或分开的头）：401，不清 cookie。"""
    async with identity_world(**_mode(secure)) as ident:
        token = await ident.login(await ident.user())
        name = ident.cookie_name
        variants = [
            [("Cookie", f"{name}=forged; {name}={token}")],
            [("Cookie", f"{name}={token}; {name}=forged")],
            [("Cookie", f"{name}={token}"), ("Cookie", f"{name}=forged")],
        ]
        for headers in variants:
            response = await ident.http.get("/v1/me", headers=headers)
            assert response.status_code == 401
            assert response.json() == UNAUTHENTICATED
            assert "set-cookie" not in response.headers
        # 只带自己那一个时照常认得
        assert (await ident.http.get("/v1/me", headers=ident.cookie(token))).status_code == 200


# --- 没配 public_url ---


async def test_without_public_url_cookies_are_not_accepted(identity_world: Any) -> None:
    """没配 public_url：带 cookie 的请求按没有凭据处理（401，不清 cookie），读写都一样。"""
    async with identity_world(public_url=None) as ident:
        admin = await ident.user(role="tenant_admin")
        token = await ident.login(admin)
        read = await ident.http.get("/v1/me", headers=ident.cookie(token))
        write = await ident.http.put(
            "/v1/secrets/s1", headers={**ident.cookie(token), **WRITE}, json={"value": "v"}
        )
        for response in (read, write):
            assert response.status_code == 401
            assert response.json() == UNAUTHENTICATED
            assert "set-cookie" not in response.headers
        # API key 不受影响
        by_key = await ident.http.get(
            "/v1/me", headers={"Authorization": f"Bearer {ident.world.admin_key}"}
        )
        assert by_key.status_code == 200


# --- 先查请求形状，再验会话 ---


async def test_act_as_header_with_cookie_is_a_bad_request(identity_world: Any) -> None:
    """用 cookie 时带 ``X-Xuanpu-Act-As``：400 invalid_request，空值、cookie 已失效也一样。"""
    async with identity_world() as ident:
        user = await ident.user()
        valid, stale = await ident.login(user), new_login_token()[0]
        for token, value in ((valid, user.user_id), (valid, ""), (stale, user.user_id)):
            response = await ident.http.get(
                "/v1/me", headers={**ident.cookie(token), "X-Xuanpu-Act-As": value}
            )
            assert response.status_code == 400
            assert response.json()["error"]["code"] == "invalid_request"
            assert "set-cookie" not in response.headers


async def test_request_shape_is_checked_before_the_session(identity_world: Any) -> None:
    """失效的 cookie 发缺自定义头的写请求：403 csrf_rejected 而不是 401——形状不对的请求不查库。"""
    async with identity_world() as ident:
        response = await ident.http.put(
            "/v1/secrets/s1", headers=ident.cookie(new_login_token()[0]), json={"value": "v"}
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "csrf_rejected"


# --- 不开放跨域 ---


async def test_no_cors_headers(identity_world: Any) -> None:
    """跨站的预检与普通请求都拿不到 ``Access-Control-Allow-*``：自定义头这道防线依赖平台不开放
    跨域，以后若有人加 CORS，这里会先红。"""
    async with identity_world() as ident:
        token = await ident.login(await ident.user(role="tenant_admin"))
        evil = {"Origin": "https://evil.example.com"}
        preflight = await ident.http.options(
            "/v1/secrets/s1",
            headers={
                **evil,
                "Access-Control-Request-Method": "PUT",
                "Access-Control-Request-Headers": "x-requested-by, content-type",
            },
        )
        read = await ident.http.get("/v1/me", headers={**evil, **ident.cookie(token)})
        assert read.status_code == 200
        for response in (preflight, read):
            assert not [
                name for name in response.headers if name.lower().startswith("access-control-")
            ]


# --- /v1/me ---


async def test_me_without_the_user_record_is_an_internal_error(
    identity_world: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """通过了认证却查不到员工记录（不该发生）：500 internal_error，细节只进错误日志。"""
    async with identity_world() as ident:
        user = await ident.user()
        token = await ident.login(user)

        async def missing(principal: Any) -> Any:
            """员工记录不见了。"""
            return None

        monkeypatch.setattr(ident.world.platform.identity, "current_user", missing)
        with caplog.at_level(logging.ERROR, logger="xuanpu_server.api.identity"):
            response = await ident.http.get("/v1/me", headers=ident.cookie(token))
        assert response.status_code == 500
        assert response.json() == {"error": {"code": "internal_error", "message": "服务内部错误"}}
        assert any(user.user_id in record.getMessage() for record in caplog.records)
