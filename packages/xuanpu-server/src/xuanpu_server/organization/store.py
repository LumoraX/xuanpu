"""组织仓储只修改协调工作单元，不另开连接或提交。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.organization.models import (
        Membership,
        OrganizationState,
        OrgRecord,
        OrgUnit,
        Position,
        RoleAssignment,
    )


def validate_organization(tenant: str, state: OrganizationState) -> None:
    """提交前统一核对引用、同级名称、循环和最大深度。"""
    groups = (state.units, state.positions, state.memberships, state.roles,
              state.authority, state.authority_previews, state.mappings, state.previews)
    for records in groups:
        for key, record in records.items():
            if record.tenant_id != tenant or key != record.id:
                raise NotFoundError('组织实体不存在')
    names: set[tuple[str | None, str]] = set()
    for unit in state.units.values():
        _validate_chain(state, unit)
        name = (unit.parent_id, unit.name.strip().casefold())
        if unit.status == 'active':
            if name in names or not name[1]:
                raise ConflictError('同级部门名称重复或为空')
            names.add(name)
    positions: set[tuple[str, str]] = set()
    for position in state.positions.values():
        if position.unit_id not in state.units:
            raise NotFoundError('部门不存在')
        name = (position.unit_id, position.name.strip().casefold())
        if position.status == 'active':
            if name in positions or not name[1]:
                raise ConflictError('同部门岗位名称重复或为空')
            positions.add(name)
    for member in state.memberships.values():
        _validate_membership(state, member)
    role_keys: set[tuple[str, str | None, str, int]] = set()
    for role in state.roles.values():
        if role.scope_id is not None and role.scope_id not in state.units:
            raise NotFoundError('职责部门不存在')
        if role.status == 'active':
            role_key = (role.user_id, role.scope_id, role.role, role.user_epoch)
            if role_key in role_keys:
                raise ConflictError('相同范围的职责已经授予该员工')
            role_keys.add(role_key)


def _validate_chain(state: OrganizationState, unit: OrgUnit) -> None:
    """从每个节点向根检查，移动祖先也会覆盖整个子树的深度。"""
    current: OrgUnit | None = unit
    seen: set[str] = set()
    while current is not None:
        if current.id in seen or len(seen) >= 16:
            raise ConflictError('部门结构不能循环或超过16层')
        seen.add(current.id)
        if current.parent_id is None:
            return
        parent = state.units.get(current.parent_id)
        if parent is None:
            raise NotFoundError('上级部门不存在')
        if current.status == 'active' and parent.status != 'active':
            raise ConflictError('启用部门不能属于停用部门')
        current = parent


def _validate_membership(state: OrganizationState, member: Membership) -> None:
    """岗位外键同时绑定租户和部门。"""
    unit = state.units.get(member.unit_id)
    if unit is None:
        raise NotFoundError('部门不存在')
    if member.position_id is not None:
        position = state.positions.get(member.position_id)
        if position is None:
            raise NotFoundError('岗位不存在')
        if position.unit_id != member.unit_id:
            raise ConflictError('岗位不属于该部门')


class OrganizationRepository:
    """受协调事务约束的类型化实体仓储。"""
    def __init__(self, transaction: CoordinationTransaction) -> None:
        """持有调用者的工作单元，永远不自行提交。"""
        self.tx = transaction

    def _check(self, record: OrgRecord) -> None:
        """拒绝跨租户记录与复用停用部门ID。"""
        if record.tenant_id != self.tx.tenant_id:
            raise NotFoundError('组织实体不存在')

    def put_unit(self, unit: OrgUnit) -> None:
        """候选结构验证成功才替换，失败不污染当前事务。"""
        self._check(unit)
        state = self.tx.organization.model_copy(deep=True)
        old = state.units.get(unit.id)
        if old is not None and old.status == 'disabled' and unit != old:
            raise ConflictError('停用部门ID不能复用')
        state.units[unit.id] = unit
        validate_organization(self.tx.tenant_id, state)
        self.tx.organization.units[unit.id] = unit

    def put_position(self, position: Position) -> None:
        """岗位变更与组织结构同一提交边界。"""
        self._check(position)
        state = self.tx.organization.model_copy(deep=True)
        state.positions[position.id] = position
        validate_organization(self.tx.tenant_id, state)
        self.tx.organization.positions[position.id] = position

    def put_membership(self, membership: Membership) -> None:
        """任职目标员工必须已按固定锁序加入事务。"""
        self._check(membership)
        self._participant(membership.user_id)
        _validate_membership(self.tx.organization, membership)
        self.tx.organization.memberships[membership.id] = membership

    def put_role(self, role: RoleAssignment) -> None:
        """业务职责仅指向当前事务明确锁住的员工及部门。"""
        self._check(role)
        self._participant(role.user_id)
        old = self.tx.organization.roles.get(role.id)
        if old is not None and old.status == 'revoked' and role != old:
            raise ConflictError('已撤销职责不能复活，请重新明确授予')
        if role.scope_id is not None and role.scope_id not in self.tx.organization.units:
            raise NotFoundError('职责部门不存在')
        state = self.tx.organization.model_copy(deep=True)
        state.roles[role.id] = role
        validate_organization(self.tx.tenant_id, state)
        self.tx.organization.roles[role.id] = role

    def _participant(self, user_id: str) -> None:
        """缺失目标锁即拒绝，防止绕过当前身份检查。"""
        if user_id not in self.tx.participants:
            raise PermissionDeniedError('任职员工未加入协调事务')
