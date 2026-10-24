"""共享授权只依据当前员工、当前任职及明确授权，不提升管理员。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError
from xuanpu_server.organization.permissions import current_memberships, descendants

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.models import SharedAccessGrant, SharedMatter
    from xuanpu_server.tenancy import Principal


def grant_active(tx: CoordinationTransaction, grant: SharedAccessGrant, user: str, now: float, *, owns: bool = False, _trail: frozenset[str] = frozenset()) -> bool:
    """部门授权要求当前任职，指定职责还须当前身份代次和范围吻合。"""
    epoch = tx.participant_epochs.get(user)
    if tx.participant_statuses.get(user) != 'active':
        return False
    if epoch is None or grant.status != 'active' or (grant.expires_at is not None and grant.expires_at <= now):
        return False
    if grant.subject_kind == 'user':
        if grant.subject_id != user or (not owns and grant.subject_epoch != epoch):
            return False
    elif grant.subject_id not in current_memberships(tx.organization, user, now, epoch):
        return False
    scope = grant.role_scope_id if grant.subject_kind == 'user' else grant.subject_id
    if scope is not None and grant.required_org_roles:
        unit = tx.organization.units.get(scope)
        if unit is None or unit.status != 'active':
            return False
        if scope not in current_memberships(tx.organization, user, now, epoch):
            return False
        roles = {a.role for a in tx.organization.roles.values() if a.user_id == user
                 and a.status == 'active' and a.user_epoch == epoch
                 and a.scope_id is not None and a.scope_id in tx.organization.units
                 and tx.organization.units[a.scope_id].status == 'active'
                 and scope in descendants(tx.organization, a.scope_id)}
        if not set(grant.required_org_roles) <= roles:
            return False
    if grant.handoff_id is not None:
        from xuanpu_server.handoffs.purpose import purpose_valid
        return purpose_valid(tx, grant, user, now, _trail)
    return True


def access(tx: CoordinationTransaction, who: Principal, identifier: str, now: float,
           *, write: bool = False, owner: bool = False, full: bool = False, _trail: frozenset[str] = frozenset()) -> tuple[SharedMatter, list[SharedAccessGrant]]:
    """所有对象越界统一404；packet成员不能编辑或枚举完整参与者。"""
    matter = tx.shared.matters.get(identifier)
    if matter is None or who.user_id is None:
        raise NotFoundError('共享事项不存在')
    owner_grants = {m.grant_id for m in tx.shared.members.values() if m.matter_id == identifier
                    and m.status == 'active' and m.role == 'owner' and m.subject_kind == 'user'
                    and m.subject_id == who.user_id == matter.owner_id}
    candidates = [g for g in tx.shared.grants.values() if g.matter_id == identifier]
    grants = [g for g in candidates if g.handoff_id is None
              and grant_active(tx, g, who.user_id, now, owns=g.id in owner_grants, _trail=_trail)]
    # 固定来源仍须返回原用途授权；新增独立full不能使旧packet足迹凭空消失。
    grants.extend(g for g in candidates if g.handoff_id is not None
        and grant_active(tx, g, who.user_id, now, _trail=_trail))
    members = [m for m in tx.shared.members.values() if m.matter_id == identifier
               and m.status == 'active' and any(g.id == m.grant_id for g in grants)]
    if not members:
        raise NotFoundError('共享事项不存在')
    if owner and (matter.owner_id != who.user_id or not any(m.role == 'owner' for m in members)):
        raise NotFoundError('共享事项不存在')
    if full or write:
        grants = [g for g in grants if g.scope == 'full' and (not write or any(
            m.grant_id == g.id and m.role in ('owner', 'editor') for m in members))]
        if not grants:
            raise NotFoundError('共享事项不存在')
    return matter, grants
