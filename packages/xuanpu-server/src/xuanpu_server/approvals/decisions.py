"""组织决定每次原生确认，顺序步骤、撤权、通知和回执原子提交。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from xuanpu_server.approvals import events
from xuanpu_server.approvals.models import ApprovalDecision
from xuanpu_server.approvals.views import receipt
from xuanpu_server.errors import ConflictError, PermissionDeniedError
from xuanpu_server.handoffs.packets import revoke_packets
from xuanpu_server.handoffs.result_checks import results
from xuanpu_server.ids import new_id
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.approvals.models import ApprovalRequest
    from xuanpu_server.approvals.schemas import ApprovalDecisionInput
    from xuanpu_server.approvals.service import ApprovalService
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff

Decision = Literal['approve','reject','return','withdraw']


async def decide(service: ApprovalService, who: Principal, identifier: str, action: Decision,
                 body: ApprovalDecisionInput) -> dict[str, JsonValue]:
    """无效当前资格先保存阻塞，不能以异常回滚撤权。"""
    who=await service.handoffs.shared.actor(who)
    assert who.user_id is not None
    payload: dict[str,JsonValue]={'action':'approval.'+action,'id':identifier,'body':body.model_dump(mode='json')}
    blocked=False
    result: dict[str,JsonValue]={}
    async with service.transaction(who,identifier) as (tx,request,handoff):
        old=tx.receipt(who.actor,body.request_id,payload)
        if old is not None:
            return old
        if body.expected_version!=request.version:
            raise ConflictError('组织审批已改变，请刷新核对')
        _authorize(request,who,action)
        reason=await service.invalid(tx,request,handoff)
        if reason is not None and action!='withdraw':
            service.block(tx,who,request,reason)
            blocked=True
        else:
            updated=_apply(service,tx,who,request,handoff,action,body.reason)
            result=receipt(updated)
            tx.record_receipt(who.actor,body.request_id,payload,result)
    if blocked:
        raise ConflictError('审批目标或冻结职责已失效，请明确重提')
    return result


def _authorize(request: ApprovalRequest, who: Principal, action: Decision) -> None:
    """当前步骤真人决定，申请人只可撤销本人尚未结束的请求。"""
    if action=='withdraw':
        if who.user_id!=request.applicant_id:
            raise PermissionDeniedError('只有申请人可以撤销本人请求')
        if request.status not in ('pending','blocked'):
            raise ConflictError('审批请求已结束')
    else:
        if request.status!='pending':
            raise ConflictError('审批请求已结束或已阻塞')
        if who.user_id==request.applicant_id or who.user_id!=request.steps[request.current_step].user_id:
            raise PermissionDeniedError('只有当前步骤指定审批人可以决定')


def _apply(service: ApprovalService, tx: CoordinationTransaction, who: Principal, request: ApprovalRequest,
           handoff: Handoff, action: Decision, reason: str) -> ApprovalRequest:
    """旧步骤权限撤销后才开放下一步；不修改交接状态，更不驱动签收。"""
    assert who.user_id is not None
    now=service.handoffs.now()
    status={'approve':'approved','reject':'rejected','return':'returned','withdraw':'withdrawn'}[action]
    current=request.current_step
    if action=='approve' and current+1<len(request.steps):
        status,current='pending',current+1
    updated=request.model_copy(update={'status':status,'current_step':current,'version':request.version+1,'updated_at':now})
    decision=ApprovalDecision(tenant_id=tx.tenant_id,id=new_id('apd'),approval_id=request.id,
        step_index=request.current_step,actor_id=who.user_id,decision=action,target_digest=request.target_digest,
        reason=reason,created_at=now,updated_at=now)
    tx.collaboration.decisions[decision.id]=decision
    revoke_packets(tx,handoff.id,now,approval_id=request.id)
    if status=='pending':
        applicant=Principal(role='tenant_member',tenant_id=tx.tenant_id,user_id=request.applicant_id,key_id=None)
        fixed=results(service.handoffs,tx,applicant,handoff,request.result_refs)
        updated=service.grant_step(tx,handoff,updated,fixed)
    tx.collaboration.approvals[request.id]=updated
    if updated.status=='pending' and not service.step_readable(tx,updated,handoff):
        updated=updated.model_copy(update={'status':'blocked','blocked_reason':'approver_result_unavailable'})
        tx.collaboration.approvals[request.id]=updated
        revoke_packets(tx,handoff.id,now,approval_id=request.id)
    service.handoffs.shared.bump(tx,tx.shared.matters[handoff.shared_id],now=now)
    events.emit(tx,who,updated,action,now)
    return updated
