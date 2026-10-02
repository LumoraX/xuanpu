"""身份相关的 HTTP 路由，挂到 ``api.app`` 的应用上。

目前只有 ``GET /v1/me``（当前调用者）；飞书登录、用户管理、飞书配置与事件回调以后也加在这里。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析路由标注
from xuanpu_server.errors import PlatformError

if TYPE_CHECKING:
    from xuanpu_server.identity.models import UserRecord
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal

logger = logging.getLogger(__name__)


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


def identity_router(platform: Platform) -> APIRouter:
    """身份相关的路由。"""
    router = APIRouter()

    @router.get("/v1/me")
    async def me(who: Caller) -> dict[str, Any]:
        """当前调用者：浏览器登录的员工、API key、代员工调用的 key 与员工，或根凭据。

        代表员工时再查一次员工记录取显示名，比认证多一次查库：这个接口只在页面加载时调一次，
        多查一次换来 ``Principal`` 不必带上身份域的记录，也不必经请求状态暗中传递。
        """
        user = await platform.identity.current_user(who)
        if who.user_id is not None and user is None:
            # 刚认证时查到过这名员工，员工记录又不会删除：走到这里是存储出了问题，按内部错误
            # 处理（500，细节只进日志），不报「不存在」误导调用方
            logger.error("员工 %s 刚通过认证，身份存储里却查不到他的记录", who.user_id)
            raise PlatformError("服务内部错误")
        return me_view(who, user)

    return router


__all__ = ["identity_router", "me_view"]
