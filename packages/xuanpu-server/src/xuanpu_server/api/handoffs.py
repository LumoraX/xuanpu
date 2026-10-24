"""到人交接原生接口，共用本人身份与Cookie CSRF保护。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from pydantic import JsonValue  # noqa: TC002 —— FastAPI响应模型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI依赖
from xuanpu_server.api.handoff_results import handoff_results_router
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.handoffs.actions import mutate
from xuanpu_server.handoffs.schemas import (  # noqa: TC001 —— FastAPI请求模型
    DeliverHandoff,
    ExplainHandoff,
    ProvideInformation,
    SignOffHandoff,
    SubmitHandoff,
    TransferHandoff,
)
from xuanpu_server.shared_matters.schemas import SharedChange  # noqa: TC001 —— 请求模型

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def handoffs_router(platform: Platform) -> APIRouter:
    """所有列表与动作由当前登录员工决定，不接受身份覆盖参数。"""
    router = APIRouter(prefix='/v1/handoffs')
    service = platform.handoffs
    router.include_router(handoff_results_router(platform))

    @router.post('', status_code=201)
    async def submit(body: SubmitHandoff, who: Caller) -> dict[str, JsonValue]:
        """明确来源、目标及精确资料包后提交交接。"""
        return await service.create(who, body)

    @router.get('')
    async def listing(request: Request, who: Caller, view: str = 'received', limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """当前发出、收到、承办或可选部门队列，数量独立于页长。"""
        if set(request.query_params) - {'view', 'limit', 'cursor'}:
            raise InvalidRequestError('交接列表不能覆盖当前身份')
        return await service.listing(who, view=view, limit=limit, cursor=cursor)

    @router.get('/{identifier}')
    async def get(identifier: str, who: Caller) -> dict[str, JsonValue]:
        """原生卡使用当前可用动作和已过滤责任历史。"""
        return await service.get(who, identifier)

    @router.post('/{identifier}/accept')
    async def accept(identifier: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """只原子认领本人，不代同事接受。"""
        return await mutate(service, who, identifier, 'accept', body)

    @router.post('/{identifier}/start')
    async def start(identifier: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """当前承办人明确开始。"""
        return await mutate(service, who, identifier, 'start', body)

    @router.post('/{identifier}/return')
    async def return_work(identifier: str, body: ExplainHandoff, who: Caller) -> dict[str, JsonValue]:
        """目标接收人说明理由退回，不复活原单。"""
        return await mutate(service, who, identifier, 'return', body)

    @router.post('/{identifier}/request-information')
    async def request_information(identifier: str, body: ExplainHandoff, who: Caller) -> dict[str, JsonValue]:
        """承办人明确缺项。"""
        return await mutate(service, who, identifier, 'request_information', body)

    @router.post('/{identifier}/provide-information')
    async def provide_information(identifier: str, body: ProvideInformation, who: Caller) -> dict[str, JsonValue]:
        """发起人明确新版本，原承办与审批不自动沿用。"""
        return await mutate(service, who, identifier, 'provide_information', body)

    @router.post('/{identifier}/cancel')
    async def cancel(identifier: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """尚未被接受的原单由发起人取消。"""
        return await mutate(service, who, identifier, 'cancel', body)

    @router.post('/{identifier}/request-cancel')
    async def request_cancel(identifier: str, body: ExplainHandoff, who: Caller) -> dict[str, JsonValue]:
        """受理后的取消需要另一次明确确认。"""
        return await mutate(service, who, identifier, 'request_cancel', body)

    @router.post('/{identifier}/confirm-cancel')
    async def confirm_cancel(identifier: str, body: SharedChange, who: Caller) -> dict[str, JsonValue]:
        """当前承办人确认取消，历史和责任不删除。"""
        return await mutate(service, who, identifier, 'confirm_cancel', body)

    @router.post('/{identifier}/transfer')
    async def transfer(identifier: str, body: TransferHandoff, who: Caller) -> dict[str, JsonValue]:
        """负责人明确指定另一员工，旧资料权限撤销，新接收人重新接受。"""
        return await mutate(service, who, identifier, 'transfer', body)

    _delivery_routes(router, platform)

    return router


def _delivery_routes(router: APIRouter, platform: Platform) -> None:
    """交付、签收、争议和重办均独立鉴权并核对固定结果。"""
    service=platform.handoffs

    @router.post('/{identifier}/deliver')
    async def deliver(identifier: str,body: DeliverHandoff,who: Caller) -> dict[str,JsonValue]:
        """当前承办人交付本次固定成果。"""
        return await mutate(service,who,identifier,'deliver',body)

    @router.post('/{identifier}/sign-off')
    async def sign_off(identifier: str, body: SignOffHandoff, who: Caller) -> dict[str, JsonValue]:
        """发起人明确签收本次交付，不由任务状态自动触发。"""
        return await mutate(service, who, identifier, 'sign_off', body)

    @router.post('/{identifier}/dispute')
    async def dispute(identifier: str,body: ExplainHandoff,who: Caller) -> dict[str,JsonValue]:
        """发起人说明争议，保留已发生的交付。"""
        return await mutate(service,who,identifier,'dispute',body)

    @router.post('/{identifier}/reopen')
    async def reopen(identifier: str,body: SharedChange,who: Caller) -> dict[str,JsonValue]:
        """当前承办人明确重办，原组织决定不授权新版本。"""
        return await mutate(service,who,identifier,'reopen',body)
