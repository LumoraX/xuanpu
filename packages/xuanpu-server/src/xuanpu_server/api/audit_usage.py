"""审计与用量的查询路由，挂到 ``api.app`` 的应用上。都只对租户管理员开放。

- ``GET /v1/audit``：查询本租户的审计记录，可按会话、类型、来源、时间、员工（``user_id``）、触发者
  （``actor``）过滤；
- ``GET /v1/usage``：本租户按天的用量；
- ``GET /v1/usage/users``：本租户按天、按员工的用量（只含有主人的会话，见设计 8.3），可只看某名
  员工的。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query

from xuanpu_server.api import schemas
from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析路由标注
from xuanpu_server.controlplane.models import AuditQuery

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform

_DAY = r"^\d{4}-\d{2}-\d{2}$"
"""用量查询的日期参数：``YYYY-MM-DD``（UTC）。"""


def audit_usage_router(platform: Platform) -> APIRouter:
    """审计与用量的查询路由。"""
    router = APIRouter()

    @router.get("/v1/audit")
    async def query_audit(  # noqa: PLR0913 —— 查询条件逐项对应查询参数
        who: Caller,
        session_id: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        source: Annotated[str | None, Query(pattern="^(engine|gate|control)$")] = None,
        since: Annotated[float | None, Query()] = None,
        until: Annotated[float | None, Query()] = None,
        user_id: Annotated[str | None, Query()] = None,
        actor: Annotated[str | None, Query()] = None,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """查询本租户的审计记录。用响应里的 ``next_after_id`` 翻页。

        ``user_id`` 只看归到这名员工名下的记录：他名下会话里的一切记录（不论是谁触发的），以及他做
        的与会话无关的控制面操作。``actor`` 只看某个触发者的（精确匹配），用来查「某个管理员或某把
        key 在别人的会话里做了什么」。成员查看自己的记录留到以后（P6），现在仍只对管理员开放。
        """
        records = await platform.store.query_audit(
            who.require_tenant_admin(),
            AuditQuery(
                session_id=session_id,
                kind=kind,
                source=source,  # type: ignore[arg-type]
                since=since,
                until=until,
                user_id=user_id,
                actor=actor,
                after_id=after_id,
                limit=limit,
            ),
        )
        return {
            "records": [schemas.audit_view(record) for record in records],
            "next_after_id": records[-1].audit_id if len(records) == limit else None,
        }

    @router.get("/v1/usage")
    async def query_usage(
        who: Caller,
        since: Annotated[str, Query(pattern=_DAY)],
        until: Annotated[str, Query(pattern=_DAY)],
    ) -> dict[str, Any]:
        """查询本租户一段日期内的用量。"""
        records = await platform.store.list_usage(
            who.require_tenant_admin(), since=since, until=until
        )
        return {"usage": [schemas.usage_view(record) for record in records]}

    @router.get("/v1/usage/users")
    async def query_user_usage(
        who: Caller,
        since: Annotated[str, Query(pattern=_DAY)],
        until: Annotated[str, Query(pattern=_DAY)],
        user_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        """查询本租户一段日期内按员工的用量，按日期、员工排序；``user_id`` 只看某名员工的。"""
        records = await platform.identity.store.list_user_usage(
            who.require_tenant_admin(), since=since, until=until, user_id=user_id
        )
        return {"usage": [schemas.user_usage_view(record) for record in records]}

    return router


__all__ = ["audit_usage_router"]
