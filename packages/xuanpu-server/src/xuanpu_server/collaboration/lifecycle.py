"""复用既有治理循环收尾失效责任与期限，绝不自动批准、认领或交付。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.approvals.lifecycle import supersede
from xuanpu_server.handoffs.events import notify
from xuanpu_server.handoffs.packets import revoke_packets
from xuanpu_server.handoffs.permissions import participant_ids
from xuanpu_server.tenancy import system_principal

if TYPE_CHECKING:
    from xuanpu_server.approvals.service import ApprovalService
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff


class CollaborationLifecycle:
    """每轮最多100单，以当前持久状态为准；扫描位置不充当事件消费水位。"""
    def __init__(self, approvals: ApprovalService) -> None:
        """沿用同一协调仓储和既有单实例治理周期，不再启动后台模型worker。"""
        self.approvals,self.handoffs=approvals,approvals.handoffs
        self._after: dict[str,str]={}

    async def recover(self, tenant: str) -> int:
        """轮转有界活跃集合；进程重启从头重扫，去重以持久事件为准。"""
        async with self.handoffs.store.transaction(tenant) as tx:
            pending={a.handoff_id for a in tx.collaboration.approvals.values() if a.status=='pending'}
            identifiers=sorted(h.id for h in tx.collaboration.handoffs.values() if h.status not in ('signed_off','returned','cancelled') or h.id in pending)
        remaining=[i for i in identifiers if i>self._after.get(tenant,'')]
        selected=(remaining if remaining else identifiers)[:100]
        for identifier in selected:
            await self._one(tenant,identifier)
            self._after[tenant]=identifier
        return len(selected)

    async def _one(self, tenant: str, identifier: str) -> None:
        """每单锁内阻塞、精确撤权和站内通知原子提交，不在批间缓存正文。"""
        async with self.handoffs.store.transaction(tenant) as read:
            users=participant_ids(read,read.collaboration.handoffs[identifier],self.handoffs.now())
        async with self.handoffs.store.transaction(tenant,participants=users,allow_inactive=users) as tx:
            handoff=tx.collaboration.handoffs[identifier]
            who=system_principal(tenant,'collaboration')
            changed=False
            if handoff.status in ('signed_off','returned','cancelled'):
                active=any(g.handoff_id==handoff.id and g.approval_id is not None and g.status=='active' for g in tx.shared.grants.values())
                supersede(tx,who,handoff.id,self.handoffs.now())
                if active:
                    self.handoffs.shared.bump(tx,tx.shared.matters[handoff.shared_id],now=self.handoffs.now())
                return
            reason=await self.handoffs.healthy(tx,handoff)
            if reason is not None:
                if handoff.blocked_reason is None:
                    handoff=self.handoffs.block(tx,who,handoff,reason)
                if any(g.handoff_id==handoff.id and g.status=='active' for g in tx.shared.grants.values()):
                    revoke_packets(tx,handoff.id,self.handoffs.now())
                    self.handoffs.shared.bump(tx,tx.shared.matters[handoff.shared_id],now=self.handoffs.now())
                    changed=True
            for request in list(tx.collaboration.approvals.values()):
                if request.handoff_id!=identifier or request.status!='pending':
                    continue
                invalid=await self.approvals.invalid(tx,request,handoff)
                if invalid is not None:
                    self.approvals.block(tx,who,request,invalid,touch_shared=not changed)
                    changed=True
            if reason is None and handoff.status not in ('delivered','signed_off','returned','cancelled'):
                self._deadline(tx,handoff)

    def _deadline(self, tx: CoordinationTransaction, handoff: Handoff) -> None:
        """同一明确期限只提醒一次，重启或并发扫描不会产生自动业务决定。"""
        if handoff.due_at is None or handoff.due_at>self.handoffs.now():
            return
        if any(e.kind=='handoff.deadline' and e.entity_id==handoff.id and e.data.get('due_at')==handoff.due_at for e in tx.state.events.values()):
            return
        now=self.handoffs.now()
        recipients={handoff.requester_id}
        if handoff.assignee_id is not None:
            recipients.add(handoff.assignee_id)
        else:
            recipients.update(self.handoffs.receivers(tx,handoff))
        event=tx.emit('handoff.deadline',handoff.id,handoff.version,actor='system:collaboration',at=now,
            data={'handoff_id':handoff.id,'due_at':handoff.due_at})
        notify(tx,tuple(sorted(recipients)),event.id,handoff.id,None,now)
