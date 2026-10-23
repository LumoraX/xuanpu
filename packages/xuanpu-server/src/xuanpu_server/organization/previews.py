"""根据已观察目录生成固定差异；不从缺失成员推导任何缩权。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from xuanpu_server.connections.account_bindings import subject
from xuanpu_server.errors import ConflictError
from xuanpu_server.ids import new_id
from xuanpu_server.organization.mapping import authority_versions, mapping
from xuanpu_server.organization.provider_snapshots import ProposedChange

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.organization.models import OrganizationState
    from xuanpu_server.organization.provider_snapshots import ProviderDepartment, ProviderSnapshot


def _ordered(departments: list[ProviderDepartment]) -> list[ProviderDepartment]:
    """来源必须是无循环、父节点可见或明确为根的有界结构。"""
    by_id = {d.id: d for d in departments}
    if len(by_id) != len(departments):
        raise ConflictError('来源部门ID重复')
    def depth(department: ProviderDepartment) -> int:
        """深度超过16或父节点循环、缺失时不猜测根节点。"""
        seen = {department.id}
        current = department
        while current.parentid not in (0, None):
            if current.parentid in seen or len(seen) >= 16 or current.parentid not in by_id:
                raise ConflictError('来源组织结构循环、超过16层或上级不在当前快照')
            seen.add(current.parentid)
            current = by_id[current.parentid]
        return len(seen)
    return sorted(departments, key=lambda d: (depth(d), d.id))


def unit_changes(state: OrganizationState, source: str, snapshot: ProviderSnapshot) -> tuple[list[ProposedChange], list[dict[str, JsonValue]], dict[int, str]]:
    """新部门分配本地稳定ID；父节点和子节点可以在同批明确选中。"""
    changes: list[ProposedChange] = []
    conflicts: list[dict[str, JsonValue]] = []
    identifiers: dict[int, str] = {}
    try:
        departments = _ordered(snapshot.departments)
    except ConflictError:
        return [], [{'code': 'source_structure_conflict'}], {}
    for department in departments:
        mapped = mapping(state, source, 'unit', str(department.id))
        local = state.units.get(mapped.local_id) if mapped else None
        if mapped and (local is None or local.status != 'active'):
            conflicts.append({'code': 'inactive_or_missing_local_unit', 'external_id': str(department.id)})
            continue
        parent = identifiers.get(department.parentid) if department.parentid not in (0, None) else None
        if department.parentid not in (0, None) and parent is None:
            conflicts.append({'code': 'unresolved_parent', 'external_id': str(department.id)})
            continue
        if not department.name or len(department.name) > 120:
            conflicts.append({'code': 'invalid_unit_name', 'external_id': str(department.id)})
            continue
        identifier = local.id if local else new_id('org')
        data: dict[str, JsonValue] = {'name': department.name, 'parent_id': parent}
        fields = tuple(k for k, v in data.items() if local is None or getattr(local, k) != v)
        identifiers[department.id] = identifier
        if not fields and mapped:
            continue
        try:
            versions = authority_versions(state, source, 'unit', fields)
        except ConflictError:
            conflicts.append({'code': 'field_authority_conflict', 'external_id': str(department.id), 'object_type': 'unit'})
            if local is None:
                identifiers.pop(department.id)
            continue
        changes.append(ProposedChange(id=new_id('chg'), object_type='unit',
            action='update' if local else 'create', external_id=str(department.id), local_id=identifier,
            expected_version=local.version if local else 0, authority_versions=versions, data=data,
            summary=(local.name + ' → ' if local else '') + department.name))
    return changes, conflicts, identifiers


async def membership_changes(state: OrganizationState, source: str, snapshot: ProviderSnapshot,
                             identifiers: dict[int, str], identity: IdentityStore, tenant: str,
                             corp: str) -> tuple[list[ProposedChange], list[dict[str, JsonValue]]]:
    """只给已明确绑定的现有员工提出任职；同名但不同userid保持未映射。"""
    changes: list[ProposedChange] = []
    conflicts: list[dict[str, JsonValue]] = []
    seen: set[tuple[int, str]] = set()
    for department in snapshot.departments:
        for member in department.members:
            key = (department.id, member.userid.lower())
            if key in seen:
                continue
            seen.add(key)
            user = await identity.get_user_by_identity(tenant, 'wecom', subject(corp, member.userid))
            if user is None or user.status != 'active':
                conflicts.append({'code': 'unmapped_user' if user is None else 'inactive_user', 'external_id': member.userid})
                continue
            unit = identifiers.get(department.id)
            if unit is None:
                continue
            external = json.dumps([member.userid.lower(), department.id], separators=(',', ':'))
            mapped = mapping(state, source, 'membership', external)
            local = state.memberships.get(mapped.local_id) if mapped else None
            if local is not None and local.status == 'active' and local.unit_id == unit and local.user_id == user.user_id:
                continue
            try:
                versions = authority_versions(state, source, 'membership', ('membership',))
            except ConflictError:
                conflicts.append({'code': 'field_authority_conflict', 'external_id': member.userid, 'object_type': 'membership'})
                continue
            if local is not None and local.status != 'active':
                conflicts.append({'code': 'revoked_membership', 'external_id': member.userid})
                continue
            changes.append(ProposedChange(id=new_id('chg'), object_type='membership',
                action='update' if local else 'create', external_id=external, local_id=local.id if local else new_id('org'),
                expected_version=local.version if local else 0, authority_versions=versions,
                data={'user_id': user.user_id, 'unit_id': unit}, summary=user.display_name + ' · ' + (department.name or '部门')))
    return changes, conflicts


async def profile_changes(state: OrganizationState, source: str, snapshot: ProviderSnapshot,
                          identity: IdentityStore, tenant: str, corp: str) -> tuple[list[ProposedChange], list[dict[str, JsonValue]]]:
    """姓名更新仅针对已明确绑定的员工；缺失及冲突字段保持未核定。"""
    changes: list[ProposedChange] = []
    conflicts: list[dict[str, JsonValue]] = []
    observed: dict[str, str] = {}
    ambiguous: set[str] = set()
    for department in snapshot.departments:
        for member in department.members:
            key = member.userid.lower()
            if member.name is None:
                continue
            if key in observed and observed[key] != member.name:
                ambiguous.add(key)
            observed[key] = member.name
    for external_id, name in observed.items():
        if external_id in ambiguous:
            conflicts.append({'code': 'conflicting_user_name', 'external_id': external_id})
            continue
        user = await identity.get_user_by_identity(tenant, 'wecom', subject(corp, external_id))
        if user is None or user.status != 'active' or user.display_name == name:
            continue
        try:
            versions = authority_versions(state, source, 'user', ('display_name',))
        except ConflictError:
            conflicts.append({'code': 'field_authority_conflict', 'external_id': external_id, 'object_type': 'user'})
            continue
        changes.append(ProposedChange(id=new_id('chg'), object_type='user', action='update',
            external_id=external_id, local_id=user.user_id, expected_version=user.profile_version,
            authority_versions=versions, summary=user.display_name + ' → ' + name,
            data={'display_name': name, 'expected_display_name': user.display_name,
                                              'user_epoch': user.lifecycle_epoch}))
    return changes, conflicts
