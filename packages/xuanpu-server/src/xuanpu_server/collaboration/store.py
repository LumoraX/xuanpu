"""两后端共享交接、组织决定和不可改写历史的提交约束。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError

if TYPE_CHECKING:
    from xuanpu_server.approvals.models import ApprovalRequest
    from xuanpu_server.collaboration.models import CollaborationState
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Delivery
    from xuanpu_server.handoffs.schemas import ResultRef
    from xuanpu_server.shared_matters.models import Publication, SharedMaterial


def validate_collaboration(tx: CoordinationTransaction) -> None:
    """领域引用与语义在同一事务验证，不把模型或客户端文字当作合法决定。"""
    for field in type(tx.collaboration).model_fields:
        for key,record in getattr(tx.collaboration,field).items():
            if record.tenant_id!=tx.tenant_id or record.id!=key:
                raise ConflictError('协作记录租户或标识不一致')
    for handoff in tx.collaboration.handoffs.values():
        _handoff(tx,handoff.id)
    _references(tx)
    _decisions(tx)
    _notifications(tx)
    _results(tx)


def _handoff(tx: CoordinationTransaction, identifier: str) -> None:
    """唯一责任人与原来源必须属于同一共享事项，历史撤权不删除固定引用。"""
    handoff=tx.collaboration.handoffs[identifier]
    source=tx.shared.grants.get(handoff.source_grant_id)
    if handoff.shared_id not in tx.shared.matters or source is None or source.matter_id!=handoff.shared_id:
        raise ConflictError('交接原来源缺失')
    if (source.scope!='full' or source.subject_kind!='user' or source.subject_id!=handoff.requester_id):
        raise ConflictError('交接来源必须属于明确发起人')
    if handoff.target_kind=='unit' and handoff.target_id not in tx.organization.units:
        raise ConflictError('交接目标部门缺失')
    if handoff.target_kind=='user' and handoff.target_epoch is None:
        raise ConflictError('到人交接需要明确员工身份代次')
    if handoff.policy_id is not None and handoff.policy_id not in tx.collaboration.policies:
        raise ConflictError('交接审批策略缺失')
    if any(i not in tx.shared.revisions or tx.shared.revisions[i].matter_id!=handoff.shared_id for i in handoff.material_revision_ids):
        raise ConflictError('交接资料不属于原事项')
    if handoff.assignee_grant_id is not None:
        grant=tx.shared.grants.get(handoff.assignee_grant_id)
        if grant is None or (grant.handoff_id,grant.subject_id)!=(handoff.id,handoff.assignee_id):
            raise ConflictError('承办人授权与责任不一致')


def _references(tx: CoordinationTransaction) -> None:
    """冻结步骤、临时用途和策略必须对应同一真实交接。"""
    state=tx.collaboration
    identifiers=[*(r.handoff_id for r in state.events.values()),*(r.handoff_id for r in state.deliveries.values()),
        *(r.handoff_id for r in state.approvals.values())]
    if any(i not in state.handoffs for i in identifiers):
        raise ConflictError('协作记录引用缺失交接')
    for request in state.approvals.values():
        policy=state.policies.get(request.policy_id)
        if policy is None or not any(r.version==request.policy_version for r in policy.revisions):
            raise ConflictError('审批引用缺失策略修订')
        if (not 1<=len(request.steps)<=3 or not 0<=request.current_step<len(request.steps)
                or tuple(s.index for s in request.steps)!=tuple(range(len(request.steps)))
                or any(s.user_id==request.applicant_id for s in request.steps)):
            raise ConflictError('审批需要一到三步明确非申请人')
    for grant in tx.shared.grants.values():
        if grant.handoff_id is not None:
            handoff=state.handoffs.get(grant.handoff_id)
            if handoff is None or handoff.shared_id!=grant.matter_id or grant.scope!='packet':
                raise ConflictError('交接授权不能扩大为整事项')
        if grant.approval_id is not None:
            approval=state.approvals.get(grant.approval_id)
            if approval is None or approval.handoff_id!=grant.handoff_id:
                raise ConflictError('审批临时授权来源不一致')


def _decisions(tx: CoordinationTransaction) -> None:
    """每步唯一真实决定，已批准必须有完整顺序证据，不能靠改status伪造。"""
    seen: set[tuple[str,int]]=set()
    for decision in tx.collaboration.decisions.values():
        request=tx.collaboration.approvals.get(decision.approval_id)
        key=(decision.approval_id,decision.step_index)
        if request is None or key in seen or decision.target_digest!=request.target_digest:
            raise ConflictError('审批决定重复或目标不一致')
        if not 0<=decision.step_index<len(request.steps):
            raise ConflictError('审批决定步骤不存在')
        actor=request.applicant_id if decision.decision=='withdraw' else request.steps[decision.step_index].user_id
        if decision.actor_id!=actor:
            raise ConflictError('审批决定人不是该步骤指定员工')
        seen.add(key)
    for request in tx.collaboration.approvals.values():
        approved={d.step_index for d in tx.collaboration.decisions.values() if d.approval_id==request.id and d.decision=='approve'}
        if not set(range(request.current_step))<=approved:
            raise ConflictError('审批不能跳过顺序步骤')
        if request.status=='approved' and approved!=set(range(len(request.steps))):
            raise ConflictError('批准缺少完整组织决定证据')


def _notifications(tx: CoordinationTransaction) -> None:
    """站内动态编号、语义键与来源顺序独立唯一。"""
    sequences: set[int]=set();semantic: set[str]=set()
    for note in tx.collaboration.notifications.values():
        if note.handoff_id not in tx.collaboration.handoffs or note.event_id not in tx.state.events:
            raise ConflictError('协作通知来源缺失')
        if note.approval_id is not None and note.approval_id not in tx.collaboration.approvals:
            raise ConflictError('审批通知来源缺失')
        if note.sequence in sequences or note.semantic_key in semantic:
            raise ConflictError('协作通知顺序或语义键重复')
        sequences.add(note.sequence);semantic.add(note.semantic_key)


def _results(tx: CoordinationTransaction) -> None:
    """固定成果SHA及受限发布关联不能指向另一事项，历史撤回仍保留字节摘要。"""
    outcomes: tuple[ApprovalRequest|Delivery,...]=(*tx.collaboration.approvals.values(),*tx.collaboration.deliveries.values())
    for outcome in outcomes:
        _fixed_refs(tx,outcome.handoff_id,outcome.result_refs)
    publications: tuple[SharedMaterial|Publication,...]=(*tx.shared.materials.values(),*tx.shared.publications.values())
    for publication in publications:
        if publication.result_for_handoff is not None:
            handoff=tx.collaboration.handoffs.get(publication.result_for_handoff)
            if handoff is None or handoff.shared_id!=publication.matter_id:
                raise ConflictError('成果用途不属于本事项')


def _fixed_refs(tx: CoordinationTransaction,handoff_id: str,refs: tuple[ResultRef,...]) -> None:
    """成果身份与SHA固定，不允许跨事项借用同名材料。"""
    if not refs or len({r.material_id for r in refs})!=len(refs):
        raise ConflictError('固定成果不能缺失或重复')
    matter=tx.collaboration.handoffs[handoff_id].shared_id
    for ref in refs:
        if not any(r.material_id==ref.material_id and r.revision==ref.revision and r.sha256==ref.sha256
                and r.matter_id==matter for r in tx.shared.revisions.values()):
            raise ConflictError('固定成果版本或SHA不一致')


def validate_history(before: CollaborationState, after: CollaborationState) -> None:
    """已提交事件、交付、决定及策略旧修订永久不可覆盖或删除。"""
    for field in type(before).model_fields:
        previous,current=getattr(before,field),getattr(after,field)
        if previous.keys()-current.keys():
            raise ConflictError('协作记录只能明确变更，不能物理删除')
        for key,old in previous.items():
            new=current[key]
            if new==old:
                continue
            if field in ('events','deliveries','decisions') or new.created_at!=old.created_at or new.version!=old.version+1:
                raise ConflictError('协作历史或记录版本不可改写')
            if field=='policies' and new.revisions[:len(old.revisions)]!=old.revisions:
                raise ConflictError('已提交策略修订不可改写')
