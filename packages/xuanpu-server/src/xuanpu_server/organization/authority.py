"""字段权威切换先预览后CAS确认；不允许供应商配置身份或审批角色。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.identity.models import UserQuery
from xuanpu_server.ids import new_id
from xuanpu_server.organization.models import AuthorityPreview, FieldAuthority

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.organization.schemas import AuthorityInput, ConfirmInput
    from xuanpu_server.organization.service import OrganizationService
    from xuanpu_server.tenancy import Principal


class AuthorityService:
    """来源变更只改字段归属，既有映射和业务历史不改写。"""
    def __init__(self, organization: OrganizationService) -> None:
        """共享组织域的当前权限、时钟和协调事务。"""
        self.org = organization

    async def _admin(self, who: Principal, *, writing: bool = False) -> Principal:
        """字段权威是企业级配置，不随部门职责转授。"""
        who = await self.org.actor(who)
        tenant = who.require_tenant_admin()
        if writing and not self.org.config.allows(tenant):
            raise PermissionDeniedError('组织新写入尚未启用')
        return who

    async def _source(self, tenant: str, source: str) -> int:
        """只接受local或实际启用并核定目录能力的安装。"""
        if source == 'local':
            return 0
        install = await self.org.control.connection_store.read_record(tenant, source)
        if (install is None or install.get('kind') != 'installation'
                or install.get('status') != 'active'
                or 'directory_preview.read' not in install.get('operations', [])):
            raise InvalidRequestError('来源必须是local或已核定目录能力的安装')
        return int(install['version'])

    async def _count(self, tx: CoordinationTransaction, object_type: str) -> int:
        """影响数量不携带员工私人正文，统计不受显示分页影响。"""
        if object_type == 'unit':
            return len(tx.organization.units)
        if object_type == 'membership':
            return len(tx.organization.memberships)
        count, after = 0, None
        while True:
            users = await self.org.identity.list_users(tx.tenant_id, UserQuery(after=after, limit=100))
            count += len(users)
            if len(users) < 100:
                return count
            after = users[-1].user_id

    async def listing(self, who: Principal) -> dict[str, JsonValue]:
        """返回显式字段规则；无规则的旧身份刷新保持既有兼容行为。"""
        who = await self._admin(who)
        async with self.org.store.transaction(who.require_tenant(), participants=(who.user_id,) if who.user_id else ()) as tx:
            who = await self._admin(who)
            return {'items': [a.model_dump(mode='json') for a in tx.organization.authority.values()],
                    'total': len(tx.organization.authority)}

    async def preview(self, who: Principal, body: AuthorityInput) -> dict[str, JsonValue]:
        """冻结当前字段规则版本及待影响实体数量，不自动应用。"""
        who = await self._admin(who, writing=True)
        tenant = who.require_tenant()
        valid = {'user': {'display_name', 'status'}, 'unit': {'name', 'parent_id', 'status'},
                 'membership': {'membership', 'status'}}
        if body.field not in valid[body.object_type]:
            raise InvalidRequestError('字段不属于该组织对象')
        async with self.org.store.transaction(tenant, participants=(who.user_id,) if who.user_id else ()) as tx:
            who = await self._admin(who)
            payload: dict[str, JsonValue] = {'action': 'authority_preview', **body.model_dump(mode='json', exclude={'request_id'})}
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            source_version = await self._source(tenant, body.source)
            policy = next((a for a in tx.organization.authority.values()
                           if a.object_type == body.object_type and a.field == body.field), None)
            now = self.org.now()
            preview = AuthorityPreview(tenant_id=tenant, id=new_id('oap'), created_at=now, updated_at=now,
                object_type=body.object_type, field=body.field, source=body.source, source_version=source_version,
                previous_version=policy.version if policy else 0, previous_source=policy.source if policy else None,
                affected_count=await self._count(tx, body.object_type))
            tx.organization.authority_previews[preview.id] = preview
            result = preview.model_dump(mode='json')
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def apply(self, who: Principal, identifier: str, body: ConfirmInput) -> dict[str, JsonValue]:
        """旧来源版本、规模或预览版本已改变时拒绝，不覆盖新配置。"""
        who = await self._admin(who, writing=True)
        tenant = who.require_tenant()
        async with self.org.store.transaction(tenant, participants=(who.user_id,) if who.user_id else ()) as tx:
            who = await self._admin(who)
            preview = tx.organization.authority_previews.get(identifier)
            if preview is None:
                raise NotFoundError('字段来源预览不存在')
            payload: dict[str, JsonValue] = {'action': 'authority_apply', 'id': identifier, 'expected_version': body.expected_version}
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            if preview.version != body.expected_version or preview.status != 'pending':
                raise ConflictError('字段来源预览已改变')
            try:
                source_version = await self._source(tenant, preview.source)
            except InvalidRequestError as exc:
                raise ConflictError('字段来源安装已改变，请重新预览') from exc
            if source_version != preview.source_version:
                raise ConflictError('字段来源安装版本已改变，请重新预览')
            policy = next((a for a in tx.organization.authority.values()
                           if a.object_type == preview.object_type and a.field == preview.field), None)
            if (policy.version if policy else 0) != preview.previous_version or await self._count(tx, preview.object_type) != preview.affected_count:
                raise ConflictError('字段归属或影响范围已改变，请重新预览')
            now = self.org.now()
            policy = FieldAuthority(tenant_id=tenant, id=policy.id if policy else new_id('ofa'),
                object_type=preview.object_type, field=preview.field, source=preview.source,
                version=preview.previous_version + 1, created_at=policy.created_at if policy else now, updated_at=now)
            tx.organization.authority[policy.id] = policy
            tx.organization.authority_previews[identifier] = preview.model_copy(update={'status': 'applied', 'version': preview.version + 1, 'updated_at': now})
            result = policy.model_dump(mode='json')
            tx.record_receipt(who.actor, body.request_id, payload, result)
            tx.emit('field_authority_changed', policy.id, policy.version, actor=who.actor, at=now)
            return result
