"""组织规则与本人审批接口，既有个人工具批准不授予组织决定权限。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应模型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.approvals import policies
from xuanpu_server.approvals.decisions import decide
from xuanpu_server.approvals.schemas import (  # noqa: TC001 —— FastAPI请求模型
    ApprovalDecisionInput,
    ChangePolicy,
    PolicyInput,
    SubmitApproval,
)
from xuanpu_server.errors import InvalidRequestError

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def organizational_approvals_router(platform: Platform) -> APIRouter:
    """配置权限和员工本人业务操作在领域事务内重新检查。"""
    router = APIRouter()
    service = platform.approvals

    @router.get('/v1/approval-policies')
    async def available(who: Caller,limit: int=50,cursor: str|None=None) -> dict[str,JsonValue]:
        """当前员工只读可选择的预配规则名称。"""
        return await policies.available(service,who,limit=limit,cursor=cursor)

    @router.get('/v1/admin/approval-policies')
    async def listing(who: Caller, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """全企业管理人查询配置及历史修订。"""
        return await policies.listing(service,who,limit=limit,cursor=cursor)

    @router.get('/v1/admin/approval-policies/{identifier}')
    async def get(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """明确读取一项策略。"""
        return await policies.listing(service,who,identifier=identifier)

    @router.post('/v1/admin/approval-policies',status_code=201)
    async def create(body: PolicyInput, who: Caller) -> dict[str, JsonValue]:
        """原生确认指定审批员工或唯一职责。"""
        return await policies.write(service,who,body)

    @router.patch('/v1/admin/approval-policies/{identifier}')
    async def change(identifier: str, body: ChangePolicy, who: Caller) -> dict[str, JsonValue]:
        """追加新修订，不覆盖已经发生的决定。"""
        return await policies.write(service,who,body,identifier)

    _requests(router, platform)
    return router


def _requests(router: APIRouter, platform: Platform) -> None:
    """本人请求与决定共用同一服务，客户端不能传入另一名操作员工。"""
    service=platform.approvals

    @router.post('/v1/approval-requests',status_code=201)
    async def submit(body: SubmitApproval, who: Caller) -> dict[str, JsonValue]:
        """只引用已冻结策略及本次ready成果。"""
        return await service.create(who,body)

    @router.get('/v1/approval-requests')
    async def listing(request: Request,who: Caller,limit: int=50,cursor: str|None=None) -> dict[str,JsonValue]:
        """列表只按当前员工，不允许身份覆盖。"""
        if set(request.query_params)-{'limit','cursor'}:
            raise InvalidRequestError('审批列表不能覆盖当前身份')
        return await service.listing(who,limit=limit,cursor=cursor)

    @router.get('/v1/approval-requests/{identifier}')
    async def get(identifier: str,who: Caller) -> dict[str,JsonValue]:
        """读取当前合法元数据和允许动作。"""
        return await service.get(who,identifier)

    @router.post('/v1/approval-requests/{identifier}/approve')
    async def approve(identifier: str,body: ApprovalDecisionInput,who: Caller) -> dict[str,JsonValue]:
        """当前步骤真人明确批准，不签收成果。"""
        return await decide(service,who,identifier,'approve',body)

    @router.post('/v1/approval-requests/{identifier}/reject')
    async def reject(identifier: str,body: ApprovalDecisionInput,who: Caller) -> dict[str,JsonValue]:
        """驳回保留决定，需要新请求重提。"""
        return await decide(service,who,identifier,'reject',body)

    @router.post('/v1/approval-requests/{identifier}/return')
    async def return_request(identifier: str,body: ApprovalDecisionInput,who: Caller) -> dict[str,JsonValue]:
        """明确退回，不复用旧批准。"""
        return await decide(service,who,identifier,'return',body)

    @router.post('/v1/approval-requests/{identifier}/withdraw')
    async def withdraw(identifier: str,body: ApprovalDecisionInput,who: Caller) -> dict[str,JsonValue]:
        """申请人撤销尚未结束的请求。"""
        return await decide(service,who,identifier,'withdraw',body)
