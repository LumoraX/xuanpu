"""组织决定只发站内来源动态，与领域修改同事务落地。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.handoffs.events import notify

if TYPE_CHECKING:
    from xuanpu_server.approvals.models import ApprovalRequest
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.tenancy import Principal


def emit(tx: CoordinationTransaction, who: Principal, request: ApprovalRequest, action: str, now: float) -> None:
    """事件不保存成果正文，收件人权限在读取时再次检查。"""
    recipients = {request.applicant_id}
    if request.status in ('pending','blocked','superseded','withdrawn'):
        recipients.add(request.steps[request.current_step].user_id)
    event = tx.emit('organizational_approval.'+action,request.id,request.version,actor=who.actor,at=now,
        data={'approval_id':request.id,'handoff_id':request.handoff_id})
    notify(tx,tuple(sorted(recipients)),event.id,request.handoff_id,who.user_id,now,approval_id=request.id)
