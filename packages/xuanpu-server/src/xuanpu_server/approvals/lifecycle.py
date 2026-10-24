"""取消或改变业务输入时，同事务结束旧未决请求，保留已有合法决定。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.approvals.events import emit
from xuanpu_server.handoffs.packets import revoke_packets

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.tenancy import Principal


def supersede(tx: CoordinationTransaction,who: Principal,handoff_id: str,now: float) -> None:
    """原输入或原单已结束，旧请求不能继续占用pending容量或接收决定。"""
    for request in list(tx.collaboration.approvals.values()):
        if request.handoff_id!=handoff_id or request.status!='pending':
            continue
        updated=request.model_copy(update={'status':'superseded','version':request.version+1,'updated_at':now})
        tx.collaboration.approvals[request.id]=updated
        revoke_packets(tx,handoff_id,now,approval_id=request.id)
        emit(tx,who,updated,'superseded',now)
