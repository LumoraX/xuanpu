"""组织请求元数据与当前动作不替代材料入口的独立用途权限。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.approvals.models import ApprovalRequest
    from xuanpu_server.approvals.service import ApprovalService
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.tenancy import Principal


def can_view(tx: CoordinationTransaction, request: ApprovalRequest, who: Principal) -> bool:
    """申请人或冻结步骤的明确员工可查看元数据，管理员没有业务正文特权。"""
    return ((who.user_id == request.applicant_id and tx.participant_epochs.get(who.user_id or '') == request.applicant_epoch)
        or any(s.user_id == who.user_id and tx.participant_epochs.get(s.user_id) == s.user_epoch for s in request.steps))


def receipt(request: ApprovalRequest) -> dict[str, JsonValue]:
    """重试回执只包含编号、状态与版本，不缓存正文。"""
    return {'id':request.id,'version':request.version,'status':request.status,
        'current_step':request.current_step,'blocked_reason':request.blocked_reason}


def readable_results(service: ApprovalService, tx: CoordinationTransaction, who: Principal, request: ApprovalRequest) -> bool:
    """终态同样核对当前成果来源，审批历史不构成永久正文授权。"""
    if request.status in ('blocked', 'superseded'):
        return False
    handoff = tx.collaboration.handoffs[request.handoff_id]
    try:
        for ref in request.result_refs:
            service.handoffs.materials.readable(tx, who, handoff.shared_id, ref.material_id, ref.revision)
    except NotFoundError:
        return False
    return True


def view(service: ApprovalService, tx: CoordinationTransaction, who: Principal, request: ApprovalRequest) -> dict[str, JsonValue]:
    """元数据中的固定版本可追溯，失效后不再提供旧结果及理由正文。"""
    actions: list[JsonValue] = []
    if request.status in ('pending','blocked') and who.user_id == request.applicant_id:
        actions.append('withdraw')
    if request.status == 'pending' and request.steps[request.current_step].user_id == who.user_id:
        actions.extend(('approve','reject','return'))
    readable = readable_results(service, tx, who, request)
    result=request.model_dump(mode='json',exclude={'steps','result_refs'})
    result['shared_id']=tx.collaboration.handoffs[request.handoff_id].shared_id
    result['can_read_results']=readable
    result['steps']=[{'index':s.index,'user_id':s.user_id} for s in request.steps]
    result['result_refs']=[] if request.status in ('blocked','superseded') else [r.model_dump(mode='json') for r in request.result_refs]
    result['allowed_actions']=actions
    result['decisions']=[{'actor_id':d.actor_id,'decision':d.decision,'step_index':d.step_index,
        'reason':d.reason if readable else ''} for d in tx.collaboration.decisions.values()
        if d.approval_id==request.id]
    return result
