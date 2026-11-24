"""玄圃自有账号的 HTTP 路由（ADR 0019），由 ``api.identity`` 挂到应用上；流程与规则在
``identity.accounts``。

不需要凭据的四个接口（登录方式、口令登录、查看与兑现设置链接）：

- ``GET /v1/auth/methods?tenant=``：登录页据此决定显示哪些登录方式；
- ``POST /v1/auth/password/login``：用户名口令登录，成功写登录 cookie；
- ``POST /v1/auth/password/setup/inspect``：设置链接是给谁的（不消耗链接）；
- ``POST /v1/auth/password/setup``：兑现设置链接、设口令，成功写登录 cookie。

三个 ``POST`` 按 cookie 写请求做防跨站检查（``identity.cookies.check_cookie_request``：必须带
``X-Requested-By: xuanpu``，带了 ``Origin`` 的必须同源），防止跨站页面把员工登录进别人的账号。

需要凭据的三个接口：

- ``POST /v1/me/password``：员工改自己的口令（浏览器登录），成功换一个新的登录 cookie；
- ``POST /v1/users``：开通员工并返回设置链接（租户管理员）；
- ``POST /v1/users/{user_id}/password-link``：给员工签发设置链接（租户管理员）。

登录、兑现与改口令的响应带 ``Cache-Control: no-store``。设置链接只在签发响应里出现一次。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from server.api import schemas
from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析路由标注
from server.core.errors import InvalidRequestError
from server.core.tenancy import validate_tenant_id
from server.domains.identity.cookies import (
    check_cookie_request,
    cookie_values,
    login_cookie_name,
    set_login_cookie,
)
from server.domains.identity.login import FEISHU
from server.domains.identity.passwords import MAX_PASSWORD_LENGTH

if TYPE_CHECKING:
    from server.domains.identity.accounts import SetupLink, SignedIn
    from server.platform import Platform


class _Body(BaseModel):
    """请求体的基类：多余的字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")


class PasswordLoginBody(_Body):
    """用户名口令登录。``tenant`` 省略时用 ``identity.default_tenant``。"""

    tenant: str | None = Field(default=None, max_length=64)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class SetupTokenBody(_Body):
    """查看设置链接：令牌取自链接片段里的 ``password_setup``。"""

    token: str = Field(min_length=1, max_length=128)


class SetupPasswordBody(SetupTokenBody):
    """兑现设置链接并设口令。"""

    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class ChangePasswordBody(_Body):
    """员工改自己的口令。"""

    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class CreateAccountBody(_Body):
    """开通员工。"""

    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    role: Literal["tenant_admin", "tenant_member"] = "tenant_member"


class PasswordLinkBody(_Body):
    """签发设置链接；员工还没开通用户名时必须给 ``username``。"""

    username: str | None = Field(default=None, min_length=1, max_length=64)


def link_view(link: SetupLink) -> dict[str, Any]:
    """设置链接的对外表示（只在签发时返回一次）。"""
    return {
        "url": link.url,
        "user_id": link.user_id,
        "username": link.username,
        "purpose": link.purpose,
        "expires_at": link.expires_at,
    }


def signed_in_response(outcome: SignedIn, *, secure: bool) -> JSONResponse:
    """登录成功：写登录 cookie，响应不进缓存。"""
    response = JSONResponse(
        {"tenant_id": outcome.tenant_id, "user_id": outcome.user_id},
        headers={"Cache-Control": "no-store"},
    )
    set_login_cookie(response, outcome.session_token, max_age=outcome.max_age, secure=secure)
    return response


def accounts_router(platform: Platform) -> APIRouter:
    """自有账号的路由。"""
    router = APIRouter()
    identity = platform.identity
    accounts = identity.accounts
    settings = identity.settings

    def guard(request: Request) -> None:
        """不需要凭据的写接口：先要求平台配了 ``public_url``，再做防跨站检查。"""
        if settings.public_url is None:
            raise InvalidRequestError(
                "平台没有配置 public_url：用户名口令登录不可用，先在服务配置里配置它"
            )
        check_cookie_request(request.method, request.headers, public_origin=settings.public_url)

    @router.get("/v1/auth/methods")
    async def auth_methods(tenant: Annotated[str | None, Query()] = None) -> dict[str, Any]:
        """租户可用的登录方式。租户不存在或标识不合法时飞书一项为 false，不透露租户是否存在。"""
        tenant_id = tenant or settings.default_tenant
        feishu = False
        if tenant_id is not None:
            try:
                validate_tenant_id(tenant_id)
            except ValueError:
                tenant_id = None
        if tenant_id is not None and settings.public_url is not None:
            feishu = await identity.store.get_identity_config(tenant_id, FEISHU) is not None
        return {"tenant": tenant_id, "password": settings.public_url is not None, "feishu": feishu}

    @router.post("/v1/auth/password/login")
    async def password_login(request: Request, body: PasswordLoginBody) -> JSONResponse:
        """用户名口令登录；浏览器还带着（恰好一个）旧登录 cookie 的，它对应的会话作废。"""
        guard(request)
        secure = settings.cookie_secure
        sessions = cookie_values(request.headers.getlist("cookie"), name=login_cookie_name(secure))
        outcome = await accounts.login(
            tenant=body.tenant,
            username=body.username,
            password=body.password,
            previous=sessions[0] if len(sessions) == 1 else None,
        )
        return signed_in_response(outcome, secure=secure)

    @router.post("/v1/auth/password/setup/inspect")
    async def inspect_setup(request: Request, body: SetupTokenBody) -> dict[str, Any]:
        """设置链接是给谁的。"""
        guard(request)
        info = await accounts.inspect_link(body.token)
        return {
            "tenant_id": info.tenant_id,
            "username": info.username,
            "display_name": info.display_name,
            "purpose": info.purpose,
            "expires_at": info.expires_at,
        }

    @router.post("/v1/auth/password/setup")
    async def redeem_setup(request: Request, body: SetupPasswordBody) -> JSONResponse:
        """兑现设置链接、设口令并登录。"""
        guard(request)
        outcome = await accounts.redeem_link(body.token, body.password)
        return signed_in_response(outcome, secure=settings.cookie_secure)

    router.include_router(_account_admin_router(platform))
    return router


def _account_admin_router(platform: Platform) -> APIRouter:
    """需要凭据的三个接口：改口令（本人）、开通与签发设置链接（租户管理员）。"""
    router = APIRouter()
    accounts = platform.identity.accounts
    settings = platform.identity.settings

    @router.post("/v1/me/password")
    async def change_password(body: ChangePasswordBody, who: Caller) -> JSONResponse:
        """改自己的口令：其他登录全部退出，当前浏览器换一个新的登录 cookie。"""
        outcome = await accounts.change_password(
            who, current=body.current_password, new=body.new_password
        )
        return signed_in_response(outcome, secure=settings.cookie_secure)

    @router.post("/v1/users", status_code=201)
    async def create_account(body: CreateAccountBody, who: Caller) -> dict[str, Any]:
        """开通员工，返回员工与设置链接。"""
        user, link = await accounts.create_account(
            who, username=body.username, display_name=body.display_name, role=body.role
        )
        return {"user": schemas.user_view(user), "setup_link": link_view(link)}

    @router.post("/v1/users/{user_id}/password-link")
    async def issue_password_link(
        user_id: str, body: PasswordLinkBody, who: Caller
    ) -> dict[str, Any]:
        """给员工签发设置链接（开通或重置）。"""
        link = await accounts.issue_link(who, user_id, username=body.username)
        return link_view(link)

    return router


__all__ = ["accounts_router", "link_view", "signed_in_response"]
