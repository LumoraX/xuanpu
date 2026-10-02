"""用户管理的 HTTP 路由，由 ``api.identity`` 挂到应用上（权限与规则在 ``identity.users``）。

- ``GET /v1/users?status=&role=&unverified=&after=&limit=``：列员工（租户管理员），按 ``user_id``
  升序，满页时 ``next_after`` 是下一页的 ``after``；
- ``GET /v1/users/{user_id}``：取员工（租户管理员或本人，别人的 404）；
- ``PUT /v1/users/{user_id}/role``：改角色（租户管理员，保住最后一名启用的管理员）；
- ``PUT /v1/users/{user_id}/status``：停用或启用（租户管理员）；停用走停用流水线，响应另带
  ``cleanup_failed``（失败的收尾，可再停用一次重跑）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Query

from xuanpu_server.api import schemas  # noqa: TC001 —— FastAPI 运行时解析请求体的标注
from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析路由标注
from xuanpu_server.identity.models import MAX_PAGE_SIZE, UserQuery

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform

DEFAULT_PAGE_SIZE = 100
"""列员工时每页默认多少条。"""


def users_router(platform: Platform) -> APIRouter:
    """用户管理的路由。"""
    router = APIRouter()
    users = platform.identity.users

    @router.get("/v1/users")
    async def list_users(  # noqa: PLR0913 —— 查询条件逐项对应查询参数
        who: Caller,
        status: Annotated[Literal["active", "disabled"] | None, Query()] = None,
        role: Annotated[Literal["tenant_admin", "tenant_member"] | None, Query()] = None,
        unverified: Annotated[bool | None, Query()] = None,
        after: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """列出本租户的员工；``unverified=true`` 只看对账时在飞书查不到的。"""
        query = UserQuery(status=status, role=role, unverified=unverified, after=after, limit=limit)
        found = await users.list_users(who, query)
        return {
            "users": [schemas.user_view(user) for user in found],
            "next_after": found[-1].user_id if len(found) == limit else None,
        }

    @router.get("/v1/users/{user_id}")
    async def get_user(user_id: str, who: Caller) -> dict[str, Any]:
        """取一名员工：管理员取谁都行，别人只能取自己。"""
        return schemas.user_view(await users.get_user(who, user_id))

    @router.put("/v1/users/{user_id}/role")
    async def set_user_role(
        user_id: str, body: schemas.UserRoleBody, who: Caller
    ) -> dict[str, Any]:
        """改员工的角色。"""
        return schemas.user_view(await users.set_role(who, user_id, body.role))

    @router.put("/v1/users/{user_id}/status")
    async def set_user_status(
        user_id: str, body: schemas.UserStatusBody, who: Caller
    ) -> dict[str, Any]:
        """停用或启用员工。"""
        user, cleanup_failed = await users.set_status(who, user_id, body.status)
        return {**schemas.user_view(user), "cleanup_failed": list(cleanup_failed)}

    return router


__all__ = ["DEFAULT_PAGE_SIZE", "users_router"]
