"""组织配置与员工目录共用当前权限、CAS和协调事务。"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.identity.models import UserQuery
from xuanpu_server.ids import new_id
from xuanpu_server.organization.authority import AuthorityService
from xuanpu_server.organization.config import OrganizationConfig
from xuanpu_server.organization.models import (
    Membership,
    OrgRecord,
    OrgUnit,
    Position,
    RoleAssignment,
)
from xuanpu_server.organization.permissions import (
    current_memberships,
    managed_units,
    manages_all,
    membership_active,
    require_manage,
)
from xuanpu_server.organization.store import OrganizationRepository
from xuanpu_server.organization.views import page
from xuanpu_server.tenancy import lower_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.coordination.store import CoordinationStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.organization.schemas import MutationInput, OrgChangeInput
    from xuanpu_server.tenancy import Principal

OrgCollection = Literal['units', 'positions', 'memberships', 'roles']
MODELS: dict[str, type[OrgRecord]] = {
    'units': OrgUnit, 'positions': Position, 'memberships': Membership, 'roles': RoleAssignment}
CHANGE_FIELDS = {'units': {'name', 'parent_id', 'status'}, 'positions': {'name', 'status'},
                 'memberships': {'position_id', 'valid_until', 'status'}, 'roles': {'status'}}

class OrganizationService:
    """管理员只能配置组织；组织目录不包含私人正文或联系方式。"""
    def __init__(self, store: CoordinationStore, identity: IdentityStore, control: ControlPlaneStore,
                 *, config: OrganizationConfig | None = None, now: Callable[[], float] = time.time) -> None:
        """注入同一控制面和身份域，不引入外部工作流引擎。"""
        self.store, self.identity, self.control = store, identity, control
        self.config = config or OrganizationConfig()
        self.now = now
        self.authority = AuthorityService(self)

    async def actor(self, who: Principal, *, employee: bool = False) -> Principal:
        """服务直接调用同样重新核对当前凭据和员工身份。"""
        tenant = who.require_tenant()
        if employee and not who.user_id:
            raise PermissionDeniedError('这个操作需要员工身份')
        role = who.role
        if who.key_id is not None:
            key = await self.control.get_api_key(who.key_id)
            if key is None or key.tenant_id != tenant or not key.active or (who.user_id is not None and not key.can_act_as_user):
                raise PermissionDeniedError('调用凭据不可用')
            role = lower_role(role, key.role)
        if who.user_id is not None:
            user = await self.identity.get_user(tenant, who.user_id)
            if user is None or user.status != 'active':
                raise PermissionDeniedError('员工不可用')
            role = lower_role(role, user.role)
        return replace(who, role=role)

    async def people(self, who: Principal, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """到人协作的最小员工目录不要求部门，不提供联系方式或私人资料。"""
        who = await self.actor(who, employee=True)
        if not 1 <= limit <= 100 or (cursor is not None and len(cursor) > 160):
            raise InvalidRequestError('分页参数无效')
        assert who.user_id is not None
        async with self.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.actor(who, employee=True)
            users = await self.identity.list_users(tx.tenant_id, UserQuery(status='active', after=cursor, limit=limit))
            total = await self.identity.count_users(tx.tenant_id, status='active')
            next_cursor = None
            if users and await self.identity.list_users(tx.tenant_id, UserQuery(status='active', after=users[-1].user_id, limit=1)):
                next_cursor = users[-1].user_id
            return {'items': [{'user_id': u.user_id, 'display_name': u.display_name} for u in users],
                    'total': total, 'next_cursor': next_cursor}

    @staticmethod
    def _records(tx: CoordinationTransaction, kind: OrgCollection) -> dict[str, OrgRecord]:
        """集合名只来自固定路由枚举。"""
        return cast('dict[str, OrgRecord]', getattr(tx.organization, kind))

    @staticmethod
    def _scope(record: OrgRecord) -> str | None:
        """权限来自实体所属范围，不能由请求另传授权范围。"""
        if isinstance(record, OrgUnit):
            return record.id
        if isinstance(record, (Position, Membership)):
            return record.unit_id
        if isinstance(record, RoleAssignment):
            return record.scope_id
        raise InvalidRequestError('未知组织实体')

    @staticmethod
    def _put(tx: CoordinationTransaction, record: OrgRecord) -> None:
        """所有领域操作通过接受同一工作单元的组织仓储。"""
        repository = OrganizationRepository(tx)
        if isinstance(record, OrgUnit):
            repository.put_unit(record)
        elif isinstance(record, Position):
            repository.put_position(record)
        elif isinstance(record, Membership):
            repository.put_membership(record)
        elif isinstance(record, RoleAssignment):
            repository.put_role(record)
        else:
            raise InvalidRequestError('未知组织实体')

    async def listing(self, who: Principal, kind: OrgCollection, *, identifier: str | None = None,
                      limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """当前范围过滤后再计算总数和翻页。"""
        who = await self.actor(who)
        tenant = who.require_tenant()
        async with self.store.transaction(tenant, participants=(who.user_id,) if who.user_id else ()) as tx:
            who = await self.actor(who)
            allowed = managed_units(tx, who)
            if not allowed and not manages_all(tx, who):
                if identifier is not None:
                    raise NotFoundError('组织记录不存在')
                raise PermissionDeniedError('没有组织管理职责')
            records = [r for r in self._records(tx, kind).values()
                       if manages_all(tx, who) or self._scope(r) in allowed]
            if identifier is not None:
                record = next((r for r in records if r.id == identifier), None)
                if record is None:
                    raise NotFoundError('组织记录不存在')
                return record.model_dump(mode='json')
            return page(records, limit=limit, cursor=cursor)

    async def create(self, who: Principal, kind: OrgCollection, body: MutationInput) -> dict[str, JsonValue]:
        """新建无旧版本；目标员工按ID排序锁住后再授任职或职责。"""
        who = await self.actor(who)
        tenant = who.require_tenant()
        if not self.config.allows(tenant):
            raise PermissionDeniedError('组织新写入尚未启用')
        payload = body.model_dump(mode='json', exclude={'request_id'})
        target = payload.get('user_id')
        participants = tuple(u for u in (who.user_id, target) if isinstance(u, str))
        async with self.store.transaction(tenant, participants=participants) as tx:
            who = await self.actor(who)
            scope = payload.get('parent_id') if kind == 'units' else payload.get('unit_id', payload.get('scope_id'))
            require_manage(tx, who, scope if isinstance(scope, str) else None)
            if kind == 'roles' and payload.get('role') == 'org_manager' and not manages_all(tx, who):
                raise PermissionDeniedError('不能转授组织管理职责')
            request: dict[str, JsonValue] = {'action': 'create', 'kind': kind, 'body': payload}
            old = tx.receipt(who.actor, body.request_id, request)
            if old is not None:
                return old
            payload = dict(payload)
            now = self.now()
            if kind == 'memberships' and payload.get('valid_from') is None:
                payload['valid_from'] = now
            if kind in ('memberships', 'roles') and isinstance(target, str):
                payload['user_epoch'] = tx.participant_epochs[target]
            record = MODELS[kind].model_validate({'tenant_id': tenant, 'id': new_id('org'),
                'created_at': now, 'updated_at': now, **payload})
            self._put(tx, record)
            result = record.model_dump(mode='json')
            tx.record_receipt(who.actor, body.request_id, request, result)
            tx.emit('membership_changed' if kind == 'memberships' else 'organization_created',
                    record.id, record.version, actor=who.actor, at=now, data={'after': result})
            return result

    async def change(self, who: Principal, kind: OrgCollection, identifier: str, body: OrgChangeInput) -> dict[str, JsonValue]:
        """请求旧版本返回409；撤销路径不受灰度关闭阻断。"""
        who = await self.actor(who)
        tenant = who.require_tenant()
        patch = body.model_dump(mode='json', exclude={'request_id', 'expected_version'}, exclude_unset=True)
        if not patch or set(patch) - CHANGE_FIELDS[kind]:
            raise InvalidRequestError('没有可变字段或字段不属于该实体')
        if not self.config.allows(tenant) and (set(patch) != {'status'} or patch.get('status') not in ('disabled', 'revoked')):
            raise PermissionDeniedError('组织新写入尚未启用')
        # 目标员工不能由请求篡改；先读取引用，再进入同锁序写事务并重新读取实体。
        async with self.store.transaction(tenant) as read:
            current = self._records(read, kind).get(identifier)
            target = getattr(current, 'user_id', None)
        participants = tuple(u for u in (who.user_id, target) if isinstance(u, str))
        inactive = (target,) if isinstance(target, str) and patch.get('status') == 'revoked' else ()
        async with self.store.transaction(tenant, participants=participants, allow_inactive=inactive) as tx:
            who = await self.actor(who)
            record = self._records(tx, kind).get(identifier)
            if record is None:
                raise NotFoundError('组织记录不存在')
            require_manage(tx, who, self._scope(record), hidden=True)
            if kind == 'units' and 'parent_id' in patch:
                parent = patch['parent_id']
                require_manage(tx, who, parent if isinstance(parent, str) else None, hidden=True)
            request: dict[str, JsonValue] = {'action': 'change', 'kind': kind, 'id': identifier,
                                           'expected_version': body.expected_version, 'body': patch}
            old = tx.receipt(who.actor, body.request_id, request)
            if old is not None:
                return old
            if record.version != body.expected_version:
                raise ConflictError('组织记录已改变，请刷新后重试')
            changes: dict[str, JsonValue] = {**record.model_dump(mode='json'), **patch,
                'version': record.version + 1, 'updated_at': self.now()}
            if isinstance(record, (Membership, RoleAssignment)):
                changes['epoch'] = record.epoch + 1
            try:
                updated = MODELS[kind].model_validate(changes)
            except ValidationError as exc:
                raise InvalidRequestError('组织变更字段无效') from exc
            self._put(tx, updated)
            result = updated.model_dump(mode='json')
            tx.record_receipt(who.actor, body.request_id, request, result)
            kind_name = 'role_revoked' if kind == 'roles' and patch.get('status') == 'revoked' else 'membership_changed' if kind == 'memberships' else 'organization_changed'
            tx.emit(kind_name, identifier, updated.version, actor=who.actor, at=self.now(),
                    data={'before': record.model_dump(mode='json'), 'after': result})
            return result

    async def me(self, who: Principal) -> dict[str, JsonValue]:
        """本人当前任职与职责，不暴露其他员工的私人资料。"""
        who = await self.actor(who, employee=True)
        tenant, user = who.require_tenant(), who.user_id
        assert user is not None
        async with self.store.transaction(tenant, participants=(user,)) as tx:
            who = await self.actor(who)
            scopes = current_memberships(tx.organization, user, self.now(), tx.participant_epochs[user])
            return {'enabled': self.config.allows(tenant),
                    'units': [{'id': unit.id, 'name': unit.name} for unit in tx.organization.units.values() if unit.id in scopes],
                    'memberships': [m.model_dump(mode='json') for m in tx.organization.memberships.values()
                                    if m.user_id == user and membership_active(tx.organization, m, self.now(), tx.participant_epochs[user])],
                    'roles': [r.model_dump(mode='json') for r in tx.organization.roles.values()
                              if r.user_id == user and r.status == 'active' and r.user_epoch == tx.participant_epochs[user]
                              and (r.scope_id is None or (r.scope_id in tx.organization.units and tx.organization.units[r.scope_id].status == 'active'))]}

    async def directory(self, who: Principal, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """仅列本部门及管理范围内的最小任职信息，无电话邮箱。"""
        who = await self.actor(who, employee=True)
        user = who.user_id
        assert user is not None
        async with self.store.transaction(who.require_tenant(), participants=(user,)) as tx:
            who = await self.actor(who)
            scopes = current_memberships(tx.organization, user, self.now(), tx.participant_epochs[user]) | managed_units(tx, who)
            records = []
            people = {}
            for member in tx.organization.memberships.values():
                if member.unit_id not in scopes:
                    continue
                current = await self.identity.get_user(who.require_tenant(), member.user_id)
                if current is not None and current.status == 'active' and membership_active(tx.organization, member, self.now(), current.lifecycle_epoch):
                    records.append(member)
                    people[member.user_id] = current
            result = page(list(records), limit=limit, cursor=cursor)
            items = result['items']
            assert isinstance(items, list)
            by_id = {m.id: m for m in records}
            for item in items:
                assert isinstance(item, dict) and isinstance(item['id'], str)
                member = by_id[item['id']]
                position = tx.organization.positions.get(member.position_id or '')
                item.update({'display_name': people[member.user_id].display_name,
                             'unit_name': tx.organization.units[member.unit_id].name,
                             'position_name': position.name if position else None})
            return result
