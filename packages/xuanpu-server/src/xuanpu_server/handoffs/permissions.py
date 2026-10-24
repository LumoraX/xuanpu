"""交接队列、固定原来源和承办资格使用当前身份，管理员没有正文特权。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError
from xuanpu_server.organization.permissions import descendants, membership_active
from xuanpu_server.shared_matters.models import SharedAccessGrant
from xuanpu_server.shared_matters.permissions import access, grant_active
from xuanpu_server.shared_matters.source_dependencies import authority_digest
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.handoffs.models import Handoff
    from xuanpu_server.shared_matters.materials import SharedMaterials


def qualifier(tx: CoordinationTransaction, unit: str, now: float, role: str = 'unit_operator') -> SharedAccessGrant:
    """只为复用已验证的资格计算，不持久授予共享内容。"""
    return SharedAccessGrant.model_validate({'tenant_id': tx.tenant_id, 'id': 'qualifier', 'matter_id': 'qualification-only',
        'subject_kind': 'unit', 'subject_id': unit, 'required_org_roles': [role], 'created_at': now, 'updated_at': now})


def eligible(tx: CoordinationTransaction, handoff: Handoff, user: str, now: float) -> bool:
    """指定员工只能本人，部门路径要求当前任职与承办职责。"""
    if tx.participant_statuses.get(user) != 'active':
        return False
    if user == handoff.requester_id:
        return False
    if handoff.target_kind == 'user':
        return handoff.target_id == user and handoff.target_epoch == tx.participant_epochs.get(user)
    return grant_active(tx, qualifier(tx, handoff.target_id, now), user, now)


def assignee_current(tx: CoordinationTransaction, handoff: Handoff, user: str, now: float) -> bool:
    """重新授予同一部门职责也不能无声恢复旧承办责任。"""
    if user != handoff.assignee_id or handoff.assignee_epoch != tx.participant_epochs.get(user) or not eligible(tx, handoff, user, now):
        return False
    if handoff.target_kind == 'unit' and handoff.assignee_authority_digest != authority_digest(tx, user, qualifier(tx, handoff.target_id, now), now):
        return False
    grant = tx.shared.grants.get(handoff.assignee_grant_id or '')
    return grant is not None and grant_active(tx, grant, user, now)


def can_view(tx: CoordinationTransaction, who: Principal, handoff: Handoff, now: float) -> bool:
    """发起人保留解困元数据，其他人只按当前明确目标和责任读取。"""
    assert who.user_id is not None
    return (who.user_id == handoff.requester_id or eligible(tx, handoff, who.user_id, now)
        or (who.user_id == handoff.assignee_id and handoff.assignee_epoch == tx.participant_epochs.get(who.user_id)))


def participant_ids(tx: CoordinationTransaction, handoff: Handoff, now: float) -> tuple[str, ...]:
    """先读取候选锁集，进入实际事务后仍重新计算当前资格。"""
    found = {handoff.requester_id}
    if handoff.assignee_id is not None:
        found.add(handoff.assignee_id)
    if handoff.target_kind == 'user':
        found.add(handoff.target_id)
    else:
        found.update(potential_operators(tx, handoff.target_id, now))
    for request in tx.collaboration.approvals.values():
        if request.handoff_id == handoff.id and request.status == 'pending':
            found.add(request.applicant_id)
            found.update(step.user_id for step in request.steps)
    return tuple(sorted(found))


async def source_valid(tx: CoordinationTransaction, handoff: Handoff, materials: SharedMaterials) -> bool:
    """只捕获明确撤权，仓储故障直接失败，不猜测可继续使用旧快照。"""
    user = await materials.service.identity.get_user(tx.tenant_id, handoff.requester_id)
    if user is None or user.status != 'active' or user.lifecycle_epoch != handoff.requester_epoch:
        return False
    owner = Principal(role='tenant_member', tenant_id=tx.tenant_id, user_id=handoff.requester_id, key_id=None)
    try:
        matter, grants = access(tx, owner, handoff.shared_id, materials.service.now(), owner=True)
        if matter.access_epoch != handoff.source_access_epoch or not any(
                g.id == handoff.source_grant_id and g.epoch == handoff.source_grant_epoch for g in grants):
            return False
        for identifier in handoff.material_revision_ids:
            revision = tx.shared.revisions.get(identifier)
            if revision is None:
                return False
            materials.readable(tx, owner, matter.id, revision.material_id, revision.revision)
    except NotFoundError:
        return False
    return True


def potential_operators(tx: CoordinationTransaction, unit: str, now: float) -> set[str]:
    """只预锁目标部门范围内可能有效的员工，当前状态仍在锁内重查。"""
    return {a.user_id for a in tx.organization.roles.values() if a.status == 'active'
        and a.role == 'unit_operator' and a.scope_id is not None and a.scope_id in tx.organization.units
        and tx.organization.units[a.scope_id].status == 'active' and unit in descendants(tx.organization, a.scope_id)
        and any(m.user_id == a.user_id and m.unit_id == unit and membership_active(tx.organization, m, now, a.user_epoch)
            for m in tx.organization.memberships.values())}


def owner_source(tx: CoordinationTransaction, shared_id: str, user: str) -> SharedAccessGrant:
    """来源必须是当前owner成员对应的独立grant，不使用无序授权列表的第一项。"""
    member = next((m for m in tx.shared.members.values() if m.matter_id == shared_id
        and m.role == 'owner' and m.status == 'active' and m.subject_id == user), None)
    if member is None:
        raise NotFoundError('来源负责人授权不存在')
    return tx.shared.grants[member.grant_id]
