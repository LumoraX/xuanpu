"""组织权限以当前职责与任职区间计算，禁止供应商授予本地角色。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import NotFoundError, PermissionDeniedError

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.organization.models import Membership, OrganizationState
    from xuanpu_server.tenancy import Principal

def descendants(state: OrganizationState, root: str) -> set[str]:
    """在已验证无循环的结构内计算当前有效子树。"""
    found = {root}
    for _ in range(16):
        children = {u.id for u in state.units.values() if u.status == 'active' and u.parent_id in found}
        if children <= found:
            break
        found |= children
    return found

def managed_units(tx: CoordinationTransaction, who: Principal) -> set[str]:
    """范围为空的组织经理拥有所有部门，其余只包含已授范围子树。"""
    state = tx.organization
    if who.role == 'tenant_admin':
        return set(state.units)
    found: set[str] = set()
    for assignment in state.roles.values():
        if assignment.user_id != who.user_id or assignment.status != 'active' or assignment.role != 'org_manager' or assignment.user_epoch != tx.participant_epochs.get(who.user_id or ''):
            continue
        if assignment.scope_id is None:
            return set(state.units)
        unit = state.units.get(assignment.scope_id)
        if unit is not None and unit.status == 'active':
            found |= descendants(state, assignment.scope_id)
    return found

def manages_all(tx: CoordinationTransaction, who: Principal) -> bool:
    """只有租户管理员或明确的企业级组织经理可以创建根部门。"""
    return who.role == 'tenant_admin' or any(
        a.user_id == who.user_id and a.status == 'active' and a.role == 'org_manager' and a.scope_id is None and a.user_epoch == tx.participant_epochs.get(who.user_id or '')
        for a in tx.organization.roles.values())

def require_manage(tx: CoordinationTransaction, who: Principal, scope: str | None, *, hidden: bool = False) -> None:
    """对象越界404；未获任何管理职责的动作403。"""
    if scope is None and manages_all(tx, who):
        return
    allowed = managed_units(tx, who)
    if scope is not None and scope in allowed:
        return
    if hidden:
        raise NotFoundError('组织记录不存在')
    raise PermissionDeniedError('没有该范围的组织管理职责')

def current_memberships(state: OrganizationState, user_id: str, now: float, user_epoch: int) -> set[str]:
    """过期、尚未生效、撤销任职及停用部门都不提供当前权限。"""
    return {m.unit_id for m in state.memberships.values()
            if m.user_id == user_id and membership_active(state, m, now, user_epoch)}


def membership_active(state: OrganizationState, member: Membership, now: float, user_epoch: int) -> bool:
    """逐条任职过滤，避免同部门旧任职因另一条新任职有效而重新出现。"""
    return (member.user_epoch == user_epoch and member.status == 'active' and member.valid_from <= now
            and (member.valid_until is None or member.valid_until > now)
            and member.unit_id in state.units and state.units[member.unit_id].status == 'active')
