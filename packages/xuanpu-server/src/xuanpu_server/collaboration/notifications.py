"""来源自有的协作收件箱，只存通用提示并按当前责任和身份投影。"""
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from xuanpu_server.approvals.views import can_view as approval_visible
from xuanpu_server.approvals.views import readable_results
from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.handoffs.permissions import can_view, participant_ids

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic import JsonValue

    from xuanpu_server.approvals.service import ApprovalService
    from xuanpu_server.collaboration.models import CollaborationNotification
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.schemas import SharedChange
    from xuanpu_server.tenancy import Principal


class CollaborationNotifications:
    """与领域动作同提交的站内动态，不复制正文、不向企微或邮箱发送消息。"""
    def __init__(self, approvals: ApprovalService) -> None:
        """复用当前协调仓储、身份和两个领域服务。"""
        self.approvals,self.handoffs=approvals,approvals.handoffs

    @asynccontextmanager
    async def transaction(self, who: Principal) -> AsyncIterator[CoordinationTransaction]:
        """整页使用同一最终事务，先确定所有相关责任人再排序锁住。"""
        assert who.user_id is not None
        async with self.handoffs.store.transaction(who.require_tenant(),participants=(who.user_id,)) as read:
            users={who.user_id}
            for handoff in read.collaboration.handoffs.values():
                if can_view(read,who,handoff,self.handoffs.now()):
                    users.update(participant_ids(read,handoff,self.handoffs.now()))
            for request in read.collaboration.approvals.values():
                if approval_visible(read,request,who):
                    users.update((request.applicant_id,*(s.user_id for s in request.steps)))
                    users.update(participant_ids(read,read.collaboration.handoffs[request.handoff_id],self.handoffs.now()))
        async with self.handoffs.store.transaction(who.require_tenant(),participants=tuple(sorted(users)),
                allow_inactive=tuple(sorted(users-{who.user_id}))) as tx:
            await self.handoffs.shared.actor(who)
            yield tx

    def visible(self, tx: CoordinationTransaction, who: Principal, identifier: str) -> CollaborationNotification:
        """旧收件人、原角色或管理员不能将通知当作永久业务内容权限。"""
        note=tx.collaboration.notifications.get(identifier)
        if note is None or note.user_id!=who.user_id or note.user_epoch!=tx.participant_epochs.get(note.user_id):
            raise NotFoundError('协作动态不存在')
        handoff=tx.collaboration.handoffs[note.handoff_id]
        allowed=can_view(tx,who,handoff,self.handoffs.now())
        if note.approval_id is not None:
            request=tx.collaboration.approvals[note.approval_id]
            allowed=approval_visible(tx,request,who)
        if not allowed:
            raise NotFoundError('协作动态不存在')
        return note

    @staticmethod
    def view(note: CollaborationNotification) -> dict[str,JsonValue]:
        """通知只带来源引用，理由、标题和成果正文须打开来源后即时授权。"""
        return {k:v for k,v in note.model_dump(mode='json').items() if k in (
            'id','version','sequence','created_at','read_at','handoff_id','approval_id')} | {
            'source':note.source_kind,'title':'组织审批有新动态' if note.approval_id else '工作交接有新动态',
            'body':'请打开查看当前可用的操作与进展。'}

    async def page(self, who: Principal, *, after_id: int=0, before_id: int|None=None,
                   limit: int=100, latest: bool=False) -> dict[str,JsonValue]:
        """当前ACL过滤后计算独立总数及未读数，游标为来源序号而非事件最大水位。"""
        who=await self.handoffs.shared.actor(who)
        if not 1<=limit<=100 or not 0<=after_id<=9223372036854775807 or (before_id is not None and not 1<=before_id<=9223372036854775807):
            raise InvalidRequestError('协作动态分页参数无效')
        async with self.transaction(who) as tx:
            visible=[]
            for note in tx.collaboration.notifications.values():
                try:
                    self.visible(tx,who,note.id)
                except NotFoundError:
                    continue
                visible.append(note)
            remaining=sorted((n for n in visible if n.sequence>after_id and (before_id is None or n.sequence<before_id)),key=lambda n:n.sequence)
            selected=remaining[-limit:] if latest else remaining[:limit]
            footprints: list[tuple[object,...]]=[]
            for handoff in tx.collaboration.handoffs.values():
                if can_view(tx,who,handoff,self.handoffs.now()):
                    footprints.append((handoff.id,handoff.requester_epoch,handoff.target_id,handoff.target_epoch,
                        handoff.assignee_id,handoff.assignee_epoch,handoff.assignee_authority_digest,handoff.input_digest,
                        await self.handoffs.healthy(tx,handoff)))
            for request in tx.collaboration.approvals.values():
                if approval_visible(tx,request,who):
                    footprints.append((request.id,request.current_step,request.status,readable_results(self.approvals,tx,who,request),
                        await self.approvals.invalid(tx,request,tx.collaboration.handoffs[request.handoff_id])))
            permission=hashlib.sha256(repr(sorted(footprints)).encode()).hexdigest()
            stamp=repr((sorted((n.id,n.version,n.read_at) for n in visible),permission))
            return {'items':[self.view(n) for n in selected],'total':len(visible),
                'unread_total':sum(n.read_at is None for n in visible),'has_more':len(remaining)>limit,
                'visibility_revision':hashlib.sha256(stamp.encode()).hexdigest(),'permission_revision':permission,
                'next_after_id':selected[-1].sequence if selected else after_id,
                'next_before_id':selected[0].sequence if selected and len(remaining)>limit else None}

    async def get(self, who: Principal, identifier: str) -> dict[str,JsonValue]:
        """外发SSE逐帧补读当前身份，不能用先前列表缓存替代。"""
        who=await self.handoffs.shared.actor(who)
        async with self.transaction(who) as tx:
            return self.view(self.visible(tx,who,identifier))

    async def read(self, who: Principal, identifier: str, body: SharedChange) -> dict[str,JsonValue]:
        """本人标读有稳定操作号及CAS，与通知记录同一次提交。"""
        who=await self.handoffs.shared.actor(who)
        payload: dict[str,JsonValue]={'action':'collaboration.notification.read','id':identifier,'body':body.model_dump(mode='json')}
        async with self.transaction(who) as tx:
            note=self.visible(tx,who,identifier)
            old=tx.receipt(who.actor,body.request_id,payload)
            if old is not None:
                return old
            if note.version!=body.expected_version:
                raise ConflictError('协作动态已改变，请刷新核对')
            if note.read_at is None:
                note=note.model_copy(update={'read_at':self.handoffs.now(),'updated_at':self.handoffs.now(),'version':note.version+1})
                tx.collaboration.notifications[note.id]=note
            result: dict[str,JsonValue]={'id':note.id,'version':note.version,'read_at':note.read_at}
            tx.record_receipt(who.actor,body.request_id,payload,result)
            return result
