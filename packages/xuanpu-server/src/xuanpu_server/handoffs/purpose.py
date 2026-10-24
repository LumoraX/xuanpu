"""交接或审批用途授权在全部共享入口即时复核，不能只保护交接卡片。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError
from xuanpu_server.shared_matters.source_dependencies import authority_digest, permission_digest
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.shared_matters.models import MaterialRevision, SharedAccessGrant


def purpose_valid(tx: CoordinationTransaction, grant: SharedAccessGrant, user: str, now: float,
                  trail: frozenset[str]) -> bool:
    """原责任足迹、当前原来源、明确用途和当前员工同时成立才授予正文。"""
    if grant.id in trail or len(trail) >= 24:
        return False
    trail = trail | {grant.id}
    handoff = tx.collaboration.handoffs.get(grant.handoff_id or '')
    if handoff is None or handoff.blocked_reason is not None or handoff.status in ('cancelled', 'returned'):
        return False
    if not _source_current(tx, handoff, now, trail) or not _assignee_current(tx, handoff, now):
        return False
    if grant.approval_id is None:
        return user == handoff.assignee_id and grant.subject_epoch == handoff.assignee_epoch
    request = tx.collaboration.approvals.get(grant.approval_id)
    if (request is None or request.status != 'pending' or request.handoff_id != handoff.id
            or request.target_version != handoff.version or not 0 <= request.current_step < len(request.steps)):
        return False
    step = request.steps[request.current_step]
    if step.user_id != user or step.grant_id != grant.id or step.user_epoch != tx.participant_epochs.get(user):
        return False
    if step.selector.selector == 'unit_role':
        from xuanpu_server.handoffs.permissions import qualifier
        from xuanpu_server.shared_matters.permissions import grant_active
        qualification = qualifier(tx, step.selector.unit_id or '', now, 'unit_approver')
        if not grant_active(tx, qualification, user, now) or authority_digest(tx, user, qualification, now) != step.authority_digest:
            return False
    return all(_result_current(tx, handoff, ref.material_id, ref.revision, ref.sha256,user,now,trail) for ref in request.result_refs)


def _source_current(tx: CoordinationTransaction, handoff: Handoff, now: float, trail: frozenset[str]) -> bool:
    """原来源绑定真正owner授权，身份恢复或无关部门授权不替代该足迹。"""
    from xuanpu_server.shared_matters.permissions import grant_active
    matter = tx.shared.matters.get(handoff.shared_id)
    source = tx.shared.grants.get(handoff.source_grant_id)
    if (matter is None or source is None or matter.owner_id != handoff.requester_id
            or source.epoch != handoff.source_grant_epoch or matter.access_epoch != handoff.source_access_epoch
            or tx.participant_statuses.get(handoff.requester_id) != 'active'
            or tx.participant_epochs.get(handoff.requester_id) != handoff.requester_epoch):
        return False
    owns = any(m.grant_id == source.id and m.role == 'owner' and m.status == 'active'
        and m.subject_id == handoff.requester_id for m in tx.shared.members.values())
    if not owns or source.scope != 'full' or source.handoff_id is not None or not grant_active(tx, source,
            handoff.requester_id, now, owns=True, _trail=trail):
        return False
    for identifier in handoff.material_revision_ids:
        revision = tx.shared.revisions.get(identifier)
        if revision is None or not _revision_current(tx, revision, handoff.requester_id, now, trail):
            return False
    return True


def _assignee_current(tx: CoordinationTransaction, handoff: Handoff, now: float) -> bool:
    """只计算原责任资格，不递归调用原packet授权造成自引用。"""
    from xuanpu_server.handoffs.permissions import eligible, qualifier
    user = handoff.assignee_id
    original = tx.shared.grants.get(handoff.assignee_grant_id or '')
    if (user is None or original is None or original.status != 'active'
            or tx.participant_statuses.get(user) != 'active'
            or handoff.assignee_epoch != tx.participant_epochs.get(user) or not eligible(tx, handoff, user, now)):
        return False
    return handoff.target_kind == 'user' or handoff.assignee_authority_digest == authority_digest(tx, user,
        qualifier(tx, handoff.target_id, now), now)


def _result_current(tx: CoordinationTransaction, handoff: Handoff, material_id: str, version: int, sha: str,
                    user: str,now: float,trail: frozenset[str]) -> bool:
    """临时审批只能读取当前目标的固定ready成果，不能混入其他事项。"""
    material = tx.shared.materials.get(material_id)
    if material is None or material.matter_id != handoff.shared_id or material.status == 'withdrawn':
        return False
    return any(r.material_id == material_id and r.revision == version and r.sha256 == sha and r.status == 'ready'
        and _revision_current(tx,r,user,now,trail) for r in tx.shared.revisions.values())


def _revision_current(tx: CoordinationTransaction, revision: MaterialRevision, user: str, now: float,
                      trail: frozenset[str]) -> bool:
    """派生资料逐级检查当前范围，有界阻断用途与材料之间的循环来源。"""
    from xuanpu_server.shared_matters.permissions import access
    if revision.id in trail or len(trail) >= 24 or revision.status != 'ready':
        return False
    material = tx.shared.materials.get(revision.material_id)
    if material is None or material.status == 'withdrawn':
        return False
    trail = trail | {revision.id}
    who = Principal(role='tenant_member', tenant_id=tx.tenant_id, user_id=user, key_id=None)
    for ref in revision.source_refs:
        try:
            matter, grants = access(tx, who, ref.shared_id, now, _trail=trail)
        except NotFoundError:
            return False
        if matter.access_epoch != ref.access_epoch:
            return False
        if not any(g.scope == 'full' for g in grants) and not (ref.source_scope == 'packet'
                and ref.permission_digest is not None and any(permission_digest(g) == ref.permission_digest for g in grants)):
            return False
        if ref.material_revision_id is not None:
            parent = tx.shared.revisions.get(ref.material_revision_id)
            if parent is None or not _revision_current(tx, parent, user, now, trail):
                return False
    return True
