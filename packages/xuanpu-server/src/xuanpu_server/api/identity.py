"""身份相关的 HTTP 路由，挂到 ``api.app`` 的应用上。

- ``GET /v1/me``：当前调用者；
- ``GET /v1/auth/feishu/login``、``GET /v1/auth/feishu/callback``：飞书登录（不需要凭据，结果都是
  302；流程见 ``identity.login``）；``POST /v1/auth/logout``：退出浏览器登录；
- ``GET /v1/identity/feishu``、``PUT /v1/identity/feishu``：租户的飞书配置；
- ``POST /v1/identity/feishu/events/{tenant_id}``：飞书事件回调（不走平台的认证，靠飞书的签名；
  流程见 ``identity.callback``）；
- 用户管理（``/v1/users``…）在 ``api.users``，经这里一并挂上，``api.app`` 不必再改。

对账以后也加在这里。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from xuanpu_server.api import schemas  # noqa: TC001 —— FastAPI 运行时解析请求体的标注
from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析路由标注
from xuanpu_server.api.users import users_router
from xuanpu_server.errors import PlatformError
from xuanpu_server.identity.cookies import (
    clear_login_attempt_cookie,
    clear_login_cookie,
    cookie_values,
    login_attempt_cookie_name,
    login_cookie_name,
    read_login_cookie,
    set_login_attempt_cookie,
    set_login_cookie,
)
from xuanpu_server.identity.events import (
    MAX_EVENT_BODY_BYTES,
    NONCE_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
)
from xuanpu_server.identity.login import LOGIN_ATTEMPT_TTL_SECONDS
from xuanpu_server.identity.urls import callback_url, event_url

if TYPE_CHECKING:
    from xuanpu_server.identity.login import LoginRedirect
    from xuanpu_server.identity.models import TenantIdentityConfig, UserRecord
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)

NO_PUBLIC_URL_HINT = (
    "平台没有配置 public_url：飞书登录不可用，回调地址与事件地址也无从生成。先在服务配置里配置"
    " public_url（见运维手册）"
)


def me_view(principal: Principal, user: UserRecord | None) -> dict[str, Any]:
    """当前调用者的对外表示。四种调用者同一个形状，用不上的字段为 ``null``。

    - ``role`` 是生效的角色：代员工调用时是 key 与员工两者中较低的那个；
    - ``key_id`` 是所用的 API key（根凭据是 ``root``），浏览器登录为 ``null``；
    - ``user_id``、``display_name`` 是代表的员工，不代表员工的 API key 与根凭据为 ``null``。
    """
    return {
        "actor": principal.actor,
        "role": principal.role,
        "tenant_id": principal.tenant_id,
        "key_id": principal.key_id,
        "user_id": principal.user_id,
        "display_name": user.display_name if user is not None else None,
    }


def feishu_config_view(
    tenant_id: str, config: TenantIdentityConfig | None, settings: IdentitySettings
) -> dict[str, Any]:
    """租户飞书配置的对外表示：只有密钥的名字，没有明文；附上要填进飞书后台的两个地址。

    还没配置时 ``configured`` 为 false、各项为 null，两个地址照样给出（先在飞书后台填好再配置）。
    平台没配 ``public_url`` 时两个地址为 null，``hint`` 说明原因。
    """
    public_url = settings.public_url
    return {
        "configured": config is not None,
        "app_id": config.app_id if config else None,
        "app_secret_ref": config.app_secret_ref if config else None,
        "encrypt_key_ref": config.encrypt_key_ref if config else None,
        "verification_token_ref": config.verification_token_ref if config else None,
        "session_ttl_seconds": config.session_ttl_seconds if config else None,
        "feishu_tenant_key": config.feishu_tenant_key if config else None,
        "callback_url": callback_url(public_url) if public_url else None,
        "event_url": event_url(public_url, tenant_id) if public_url else None,
        "hint": None if public_url else NO_PUBLIC_URL_HINT,
    }


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


def identity_router(platform: Platform) -> APIRouter:
    """身份相关的路由（含 ``api.users`` 的用户管理）。"""
    router = APIRouter()
    identity = platform.identity
    router.include_router(users_router(platform))

    @router.get("/v1/me")
    async def me(who: Caller) -> dict[str, Any]:
        """当前调用者：浏览器登录的员工、API key、代员工调用的 key 与员工，或根凭据。

        代表员工时再查一次员工记录取显示名，比认证多一次查库：这个接口只在页面加载时调一次，
        多查一次换来 ``Principal`` 不必带上身份域的记录，也不必经请求状态暗中传递。
        """
        user = await identity.current_user(who)
        if who.user_id is not None and user is None:
            # 刚认证时查到过这名员工，员工记录又不会删除：走到这里是存储出了问题，按内部错误
            # 处理（500，细节只进日志），不报「不存在」误导调用方
            logger.error("员工 %s 刚通过认证，身份存储里却查不到他的记录", who.user_id)
            raise PlatformError("服务内部错误")
        return me_view(who, user)

    @router.get("/v1/auth/feishu/login")
    async def feishu_login(
        tenant: Annotated[str | None, Query()] = None,
        return_to: Annotated[str | None, Query()] = None,
    ) -> RedirectResponse:
        """开始飞书登录：302 到飞书授权页（失败时 302 回落地页，带 ``login_error``）。"""
        outcome = await identity.login.begin(tenant=tenant, return_to=return_to)
        return login_response(outcome, secure=identity.settings.cookie_secure, clear_attempt=False)

    @router.get("/v1/auth/feishu/callback")
    async def feishu_callback(
        request: Request,
        state: Annotated[str | None, Query()] = None,
        code: Annotated[str | None, Query()] = None,
        error: Annotated[str | None, Query()] = None,
    ) -> RedirectResponse:
        """飞书授权页跳回：完成登录，302 到 ``return_to``（失败时回落地页）；总清掉登录尝试 cookie。

        登录尝试 cookie 同名不止一个时按没带处理（``state_invalid``）。浏览器还带着（恰好一个）
        旧的登录 cookie 的，登录成功后它对应的登录会话作废。
        """
        secure = identity.settings.cookie_secure
        headers = request.headers.getlist("cookie")
        attempts = cookie_values(headers, name=login_attempt_cookie_name(secure))
        sessions = cookie_values(headers, name=login_cookie_name(secure))
        outcome = await identity.login.complete(
            state=state,
            code=code,
            error=error,
            attempt_cookie=attempts[0] if len(attempts) == 1 else None,
            session_cookie=sessions[0] if len(sessions) == 1 else None,
        )
        return login_response(outcome, secure=secure, clear_attempt=True)

    @router.post("/v1/auth/logout")
    async def logout(request: Request, who: Caller) -> JSONResponse:
        """退出浏览器登录：删掉当前登录会话，清 cookie。"""
        secure = identity.settings.cookie_secure
        token = read_login_cookie(request.headers.getlist("cookie"), name=login_cookie_name(secure))
        await identity.logout(who, token)
        response = JSONResponse({"logged_out": True})
        clear_login_cookie(response, secure=secure)
        return response

    @router.get("/v1/identity/feishu")
    async def get_feishu_config(who: Caller) -> dict[str, Any]:
        """本租户的飞书配置（不含密钥明文），以及回调地址、事件地址。"""
        config = await identity.feishu_config(who)
        return feishu_config_view(who.require_tenant(), config, identity.settings)

    @router.put("/v1/identity/feishu")
    async def put_feishu_config(body: schemas.FeishuConfigBody, who: Caller) -> dict[str, Any]:
        """写入或整体替换本租户的飞书配置。"""
        config = await identity.put_feishu_config(
            who,
            app_id=body.app_id,
            app_secret_ref=body.app_secret_ref,
            encrypt_key_ref=body.encrypt_key_ref,
            verification_token_ref=body.verification_token_ref,
            session_ttl_seconds=body.session_ttl_seconds,
        )
        return feishu_config_view(config.tenant_id, config, identity.settings)

    @router.post("/v1/identity/feishu/events/{tenant_id}")
    async def feishu_events(tenant_id: str, request: Request) -> dict[str, Any]:
        """飞书事件回调：URL 验证回 ``challenge``，离职事件停用员工，别的事件 200 忽略。

        不走平台的认证（没有 ``Caller``）：来源靠飞书的签名认。请求体最多读 1 MiB 多一个字节。
        """
        headers = request.headers
        return await identity.events.handle(
            tenant_id,
            body=await read_bounded(request, MAX_EVENT_BODY_BYTES),
            signature=headers.get(SIGNATURE_HEADER),
            timestamp=headers.get(TIMESTAMP_HEADER),
            nonce=headers.get(NONCE_HEADER),
        )

    return router


__all__ = ["feishu_config_view", "identity_router", "login_response", "me_view", "read_bounded"]
