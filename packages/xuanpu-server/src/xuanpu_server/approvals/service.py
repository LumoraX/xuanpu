"""固定策略与成果的顺序真人审批，组织批准与个人工具批准分别记录。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from xuanpu_server.approvals import events
from xuanpu_server.approvals.models import ApprovalRequest
from xuanpu_server.approvals.resolver import candidates, resolve, step_current
from xuanpu_server.approvals.views import can_view, receipt, view
from xuanpu_server.collaboration.models import CollaborationConfig
from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.handoffs.packets import grant_packet, revoke_packets
from xuanpu_server.handoffs.permissions import participant_ids
from xuanpu_server.handoffs.result_checks import results, target_digest
from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.views import page
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pydantic import JsonValue

    from xuanpu_server.approvals.models import ApprovalPolicy
    from xuanpu_server.approvals.schemas import SubmitApproval
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.handoffs.service import HandoffService


class ApprovalService:
    """审批流程复用交接、当前身份和协调事务，不建立无人授权主体。"""
    def __init__(self, handoffs: HandoffService, *, config: CollaborationConfig | None = None) -> None:
        """灰度只控制新请求，既有业务决定仍逐次检查当前权限。"""
        self.handoffs, self.config = handoffs, config or CollaborationConfig()

    async def create(self, who: Principal, body: SubmitApproval) -> dict[str, JsonValue]:
        """策略解析、目标足迹、临时权限、回执和通知同一次事务提交。"""
        who = await self.handoffs.shared.actor(who)
        assert who.user_id is not None
        async with self.handoffs.transaction(who,body.target_id) as (read,handoff):
            policy=read.collaboration.policies.get(body.policy_id)
            if policy is None or not any(r.version==handoff.policy_version for r in policy.revisions):
                raise ConflictError('原策略修订不存在')
            revision=next(r for r in policy.revisions if r.version==handoff.policy_version)
            users=candidates(read,revision.steps)
        payload: dict[str, JsonValue]={'action':'approval.submit','body':body.model_dump(mode='json')}
        async with self.handoffs.transaction(who,body.target_id,extra=tuple(sorted(users))) as (tx,handoff):
            old=tx.receipt(who.actor,body.request_id,payload)
            if old is not None:
                return old
            if not self.config.allows(tx.tenant_id):
                raise PermissionDeniedError('新组织审批尚未启用')
            policy=self._policy(tx,handoff,body)
            if who.user_id!=handoff.assignee_id or handoff.status!='in_progress' or handoff.cancel_requested:
                raise PermissionDeniedError('只有当前处理中承办人可以申请成果审批')
            if await self.handoffs.healthy(tx,handoff) is not None:
                raise ConflictError('交接资格或来源已失效，请重新安排')
            if handoff.version!=body.target_version:
                raise ConflictError('审批目标已变化')
            if any(a.handoff_id==handoff.id and a.status=='pending' for a in tx.collaboration.approvals.values()):
                raise ConflictError('该交接已有未决审批，请先处理或撤销')
            if sum(a.status=='pending' for a in tx.collaboration.approvals.values())>=self.config.max_active:
                raise ConflictError('当前未决审批达到上限')
            revision=next(r for r in policy.revisions if r.version==handoff.policy_version)
            steps=resolve(tx,revision.steps,who.user_id,self.handoffs.now())
            fixed=results(self.handoffs,tx,who,handoff,body.result_refs)
            now=self.handoffs.now()
            request=ApprovalRequest(tenant_id=tx.tenant_id,id=new_id('apr'),policy_id=policy.id,
                policy_version=revision.version,applicant_id=who.user_id,applicant_epoch=tx.participant_epochs[who.user_id],
                handoff_id=handoff.id,target_version=handoff.version,target_digest=target_digest(handoff,body.result_refs),
                result_refs=body.result_refs,steps=steps,created_at=now,updated_at=now)
            request=self.grant_step(tx,handoff,request,fixed)
            tx.collaboration.approvals[request.id]=request
            if not self.step_readable(tx,request,handoff):
                raise ConflictError('当前审批人无法读取固定成果的完整来源，请原来源负责人明确授权后重提')
            self.handoffs.shared.bump(tx,tx.shared.matters[handoff.shared_id],now=now)
            events.emit(tx,who,request,'submit',now)
            result=receipt(request)
            tx.record_receipt(who.actor,body.request_id,payload,result)
            return result

    @staticmethod
    def _policy(tx: CoordinationTransaction, handoff: Handoff, body: SubmitApproval) -> ApprovalPolicy:
        """申请只能使用原生交接冻结的策略修订，不能自行换成容易通过的规则。"""
        policy=tx.collaboration.policies.get(body.policy_id)
        if (policy is None or policy.status!='active' or handoff.policy_id!=policy.id or handoff.policy_version!=policy.version
                or not any(r.version==handoff.policy_version for r in policy.revisions)):
            raise ConflictError('交接没有这项有效的冻结审批策略')
        return policy

    def grant_step(self, tx: CoordinationTransaction, handoff: Handoff, request: ApprovalRequest,
                   revisions: tuple[str,...]) -> ApprovalRequest:
        """只当前步骤取得成果packet，不包含原事项正文或后续步骤提前读取。"""
        step=request.steps[request.current_step]
        grant=grant_packet(tx,handoff,step.user_id,self.handoffs.now(),revisions=revisions,approval_id=request.id)
        steps=tuple(s.model_copy(update={'grant_id':grant}) if s.index==step.index else s for s in request.steps)
        return request.model_copy(update={'steps':steps})

    @asynccontextmanager
    async def transaction(self, who: Principal, identifier: str) -> AsyncIterator[tuple[CoordinationTransaction,ApprovalRequest,Handoff]]:
        """审批人可只有临时成果权限，无需被提升为原交接承办人。"""
        assert who.user_id is not None
        async with self.handoffs.store.transaction(who.require_tenant(),participants=(who.user_id,)) as read:
            request=read.collaboration.approvals.get(identifier)
            if request is None or not can_view(read,request,who):
                raise NotFoundError('审批请求不存在')
            handoff=read.collaboration.handoffs[request.handoff_id]
            users={who.user_id,request.applicant_id,*(s.user_id for s in request.steps),
                *participant_ids(read,handoff,self.handoffs.now())}
        async with self.handoffs.store.transaction(who.require_tenant(),participants=tuple(sorted(users)),
                allow_inactive=tuple(sorted(users-{who.user_id}))) as tx:
            await self.handoffs.shared.actor(who)
            request=tx.collaboration.approvals[identifier]
            if not can_view(tx,request,who):
                raise NotFoundError('审批请求不存在')
            yield tx,request,tx.collaboration.handoffs[request.handoff_id]

    async def invalid(self, tx: CoordinationTransaction, request: ApprovalRequest, handoff: Handoff) -> str | None:
        """未决步骤即时核对原责任与每个冻结员工，不能自动换审批人。"""
        if request.status!='pending':
            return request.blocked_reason
        if handoff.version!=request.target_version or target_digest(handoff,request.result_refs)!=request.target_digest:
            return 'target_changed'
        if await self.handoffs.healthy(tx,handoff) is not None:
            return 'handoff_unavailable'
        if tx.participant_epochs.get(request.applicant_id)!=request.applicant_epoch or tx.participant_statuses.get(request.applicant_id)!='active':
            return 'applicant_unavailable'
        if any(not step_current(tx,s,self.handoffs.now()) for s in request.steps[request.current_step:]):
            return 'approver_authority_revoked'
        applicant=Principal(role='tenant_member',tenant_id=tx.tenant_id,user_id=request.applicant_id,key_id=None)
        try:
            results(self.handoffs,tx,applicant,handoff,request.result_refs)
        except (NotFoundError,ConflictError):
            return 'result_unavailable'
        if not self.step_readable(tx,request,handoff):
            return 'approver_result_unavailable'
        return None

    def step_readable(self, tx: CoordinationTransaction, request: ApprovalRequest, handoff: Handoff) -> bool:
        """临时packet不替代派生物全部原来源权限，审批本人必须实际可读成果。"""
        step=request.steps[request.current_step]
        approver=Principal(role='tenant_member',tenant_id=tx.tenant_id,user_id=step.user_id,key_id=None)
        try:
            results(self.handoffs,tx,approver,handoff,request.result_refs)
        except (NotFoundError,ConflictError):
            return False
        return True

    def block(self, tx: CoordinationTransaction, who: Principal, request: ApprovalRequest, reason: str, *, touch_shared: bool = True) -> ApprovalRequest:
        """阻塞状态及临时撤权保存后再返回，身份恢复不复活旧步骤。"""
        if request.status!='pending':
            return request
        updated=request.model_copy(update={'status':'blocked','blocked_reason':reason,
            'version':request.version+1,'updated_at':self.handoffs.now()})
        tx.collaboration.approvals[request.id]=updated
        revoke_packets(tx,request.handoff_id,self.handoffs.now(),approval_id=request.id)
        if touch_shared:
            self.handoffs.shared.bump(tx,tx.shared.matters[tx.collaboration.handoffs[request.handoff_id].shared_id],now=self.handoffs.now())
        events.emit(tx,who,updated,'blocked',self.handoffs.now())
        return updated

    async def get(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """查询前即时检查来源和冻结责任，不靠后台清理保护正文。"""
        who=await self.handoffs.shared.actor(who)
        async with self.transaction(who,identifier) as (tx,request,handoff):
            reason=await self.invalid(tx,request,handoff)
            if reason is not None:
                request=self.block(tx,who,request,reason)
            return view(self,tx,who,request)

    async def listing(self, who: Principal, *, limit: int=50, cursor: str|None=None) -> dict[str, JsonValue]:
        """仅列当前员工参与的审批，先核对并过滤再计算分页数量。"""
        who=await self.handoffs.shared.actor(who)
        assert who.user_id is not None
        async with self.handoffs.store.transaction(who.require_tenant(),participants=(who.user_id,)) as read:
            identifiers=sorted(a.id for a in read.collaboration.approvals.values() if can_view(read,a,who))
            users={who.user_id}
            for identifier in identifiers:
                request=read.collaboration.approvals[identifier]
                users.update((request.applicant_id,*(s.user_id for s in request.steps)))
                users.update(participant_ids(read,read.collaboration.handoffs[request.handoff_id],self.handoffs.now()))
        async with self.handoffs.store.transaction(who.require_tenant(),participants=tuple(sorted(users)),
                allow_inactive=tuple(sorted(users-{who.user_id}))) as tx:
            await self.handoffs.shared.actor(who)
            records: list[dict[str,JsonValue]]=[]
            touched: set[str]=set()
            for identifier in identifiers:
                request=tx.collaboration.approvals[identifier]
                if not can_view(tx,request,who):
                    continue
                handoff=tx.collaboration.handoffs[request.handoff_id]
                reason=await self.invalid(tx,request,handoff)
                if reason is not None and request.status=='pending':
                    request=self.block(tx,who,request,reason,touch_shared=handoff.shared_id not in touched)
                    touched.add(handoff.shared_id)
                records.append(view(self,tx,who,request))
            return {**page(records,limit=limit,cursor=cursor),'enabled':self.config.allows(who.require_tenant())}
