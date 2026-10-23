"""供应商采集、差异预览与选中批次应用分离，HTTP调用从不持有协调锁。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.connections.models import ConnectionRef
from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.ids import new_id
from xuanpu_server.organization.mapping import authority_versions, mapping
from xuanpu_server.organization.models import Membership, OrgUnit, SourceMapping, SyncPreview
from xuanpu_server.organization.previews import membership_changes, profile_changes, unit_changes
from xuanpu_server.organization.provider_snapshots import ProviderSnapshot
from xuanpu_server.organization.store import OrganizationRepository
from xuanpu_server.organization.user_profiles import ProfileUpdate

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.connections.broker import CredentialBroker
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.organization.provider_snapshots import ProposedChange
    from xuanpu_server.organization.schemas import ImportApplyInput, ImportInput
    from xuanpu_server.organization.service import OrganizationService
    from xuanpu_server.tenancy import Principal


def preview_view(preview: SyncPreview) -> dict[str, JsonValue]:
    """展示来源证据与固定差异，不公开内部委托引用或凭据。"""
    result = preview.model_dump(mode='json', exclude={'snapshot', 'connection_ref'})
    result.update(preview.snapshot.model_dump(mode='json'))
    return result


class OrganizationSync:
    """没有明确本地身份映射的员工只列冲突，不JIT建人或按姓名合并。"""
    def __init__(self, organization: OrganizationService, broker: CredentialBroker) -> None:
        """复用A的本人同意及逐请求出口检查。"""
        self.org, self.broker = organization, broker

    async def _actor(self, who: Principal, *, writing: bool = True) -> Principal:
        """企业级目录源需企业管理员明确配置；不借部门职责扩大外部范围。"""
        who = await self.org.actor(who)
        tenant = who.require_tenant_admin()
        if writing and not self.org.config.allows(tenant):
            raise PermissionDeniedError('组织新写入尚未启用')
        return who

    async def create(self, who: Principal, body: ImportInput) -> dict[str, JsonValue]:
        """先查幂等，再事务外采集；最后固定本地版本和差异。"""
        who = await self._actor(who)
        tenant = who.require_tenant()
        payload: dict[str, JsonValue] = {'action': 'directory_preview', 'connection_id': body.connection_id}
        participants = (who.user_id,) if who.user_id else ()
        async with self.org.store.transaction(tenant, participants=participants) as tx:
            who = await self._actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
        captured = await self.broker.read(who, body.connection_id, 'directory_preview.read', {})
        snapshot = ProviderSnapshot.model_validate(captured)
        ref = ConnectionRef.model_validate(captured['connection_ref'])
        async with self.org.control.connection_store.transaction(tenant) as state:
            install, _ = self.broker.service.reference(state, body.connection_id, who.user_id or '', 'directory_preview.read', ref)
            corp = str(install['corp_id'])
        async with self.org.store.transaction(tenant, participants=participants) as tx:
            who = await self._actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            units, conflicts, identifiers = unit_changes(tx.organization, body.connection_id, snapshot)
            memberships, member_conflicts = await membership_changes(tx.organization, body.connection_id, snapshot,
                identifiers, self.org.identity, tenant, corp)
            profiles, profile_conflicts = await profile_changes(tx.organization, body.connection_id, snapshot,
                self.org.identity, tenant, corp)
            now = self.org.now()
            preview = SyncPreview(tenant_id=tenant, id=new_id('osp'), created_at=now, updated_at=now,
                source=body.connection_id, source_revision=snapshot.source_revision, complete=False,
                snapshot=snapshot, connection_ref=ref, changes=tuple(units + memberships + profiles),
                conflicts=tuple(conflicts + member_conflicts + profile_conflicts))
            tx.organization.previews[preview.id] = preview
            result = preview_view(preview)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            tx.emit('directory_preview_created', preview.id, 1, actor=who.actor, at=now)
            return result

    async def get(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """兼容读取历史预览；关闭新写入不删除证据。"""
        who = await self._actor(who, writing=False)
        async with self.org.store.transaction(who.require_tenant(), participants=(who.user_id,) if who.user_id else ()) as tx:
            await self._actor(who, writing=False)
            preview = tx.organization.previews.get(identifier)
            if preview is None:
                raise NotFoundError('组织导入预览不存在')
            return preview_view(preview)

    async def apply(self, who: Principal, identifier: str, body: ImportApplyInput) -> dict[str, JsonValue]:
        """来源重新采集后按固定版本提交选中批次，不宣称跨批次全局回滚。"""
        who = await self._actor(who)
        tenant = who.require_tenant()
        payload: dict[str, JsonValue] = {'action': 'directory_apply', 'id': identifier,
                                       **body.model_dump(mode='json', exclude={'request_id'})}
        async with self.org.store.transaction(tenant) as tx:
            who = await self._actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            preview = tx.organization.previews.get(identifier)
            if preview is None:
                raise NotFoundError('组织导入预览不存在')
            self._selection(preview, body)
        fresh = await self.broker.read(who, preview.source, 'directory_preview.read', {}, ref=preview.connection_ref)
        if ProviderSnapshot.model_validate(fresh).source_revision != preview.source_revision:
            raise ConflictError('供应商目录已改变，请重新采集预览')
        selected = self._selection(preview, body)
        users = tuple(str(c.data['user_id']) if c.object_type == 'membership' else c.local_id
                      for c in selected if c.object_type in ('membership', 'user'))
        participants = users + ((who.user_id,) if who.user_id else ())
        async with self.org.store.transaction(tenant, participants=participants) as tx:
            who = await self._actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            current = tx.organization.previews.get(identifier)
            if current is None:
                raise NotFoundError('组织导入预览不存在')
            selected = self._selection(current, body)
            for change in selected:
                self._apply_change(tx, current.source, change, who)
            applied = tuple(dict.fromkeys((*current.applied_changes, *(c.id for c in selected))))
            status = 'applied' if len(applied) == len(current.changes) else 'pending'
            updated = current.model_copy(update={'version': current.version + 1, 'updated_at': self.org.now(),
                                                'applied_changes': applied, 'status': status})
            tx.organization.previews[identifier] = updated
            result = preview_view(updated)
            tx.record_receipt(who.actor, body.request_id, payload, result)
            tx.emit('directory_batch_applied', identifier, updated.version, actor=who.actor, at=self.org.now())
            return result

    @staticmethod
    def _selection(preview: SyncPreview, body: ImportApplyInput) -> list[ProposedChange]:
        """不接受客户端虚构变更、改变操作类型或重复选择。"""
        if preview.version != body.expected_version or preview.source_revision != body.source_revision or preview.status != 'pending':
            raise ConflictError('预览版本或来源摘要已改变')
        known = {c.id: c for c in preview.changes}
        ids = [s.change_id for s in body.selected_changes]
        if len(ids) != len(set(ids)):
            raise ConflictError('同一批不能重复选择变更')
        for selection in body.selected_changes:
            change = known.get(selection.change_id)
            if change is None or change.action != selection.action or change.id in preview.applied_changes:
                raise ConflictError('变更不存在、已应用或操作类型不匹配')
        chosen = set(ids)
        # 采用预览固定的父部门→子部门→任职顺序，不依赖客户端提交顺序。
        return [c for c in preview.changes if c.id in chosen]

    def _apply_change(self, tx: CoordinationTransaction, source: str, change: ProposedChange, who: Principal) -> None:
        """一个连接提交组织实体、稳定映射、回执和治理事件。"""
        versions = authority_versions(tx.organization, source, change.object_type, tuple(change.authority_versions))
        if versions != change.authority_versions:
            raise ConflictError('字段来源版本已改变')
        if change.object_type == 'user':
            tx.stage_profile(ProfileUpdate.model_validate({'tenant_id': tx.tenant_id,
                'user_id': change.local_id, 'expected_epoch': change.data['user_epoch'],
                'expected_version': change.expected_version, 'expected_name': change.data['expected_display_name'],
                'display_name': change.data['display_name'], 'source': source}))
            tx.emit('profile_changed', change.local_id, change.expected_version + 1,
                    actor=who.actor, at=self.org.now(), data={'before_name': change.data['expected_display_name'],
                                                           'after_name': change.data['display_name']})
            return
        records = tx.organization.units if change.object_type == 'unit' else tx.organization.memberships
        old = records.get(change.local_id)
        if (old.version if old else 0) != change.expected_version:
            raise ConflictError('本地组织记录已改变')
        prior_mapping = mapping(tx.organization, source, change.object_type, change.external_id)
        if prior_mapping is not None and prior_mapping.local_id != change.local_id:
            raise ConflictError('来源映射已改变')
        now = self.org.now()
        values = {'tenant_id': tx.tenant_id, 'id': change.local_id, 'created_at': old.created_at if old else now,
                  'updated_at': now, 'version': change.expected_version + 1, **change.data}
        repository = OrganizationRepository(tx)
        if change.object_type == 'unit':
            parent = change.data.get('parent_id')
            if parent is not None and (not isinstance(parent, str) or parent not in tx.organization.units):
                raise ConflictError('本批未包含尚未导入的上级部门，请明确选择前置变更')
            repository.put_unit(OrgUnit.model_validate(values))
        else:
            values['user_epoch'] = tx.participant_epochs[str(change.data['user_id'])]
            values['valid_from'] = old.valid_from if isinstance(old, Membership) else now
            values['epoch'] = old.epoch + 1 if isinstance(old, Membership) else 1
            repository.put_membership(Membership.model_validate(values))
        if prior_mapping is None:
            link = SourceMapping(tenant_id=tx.tenant_id, id=new_id('osm'), source=source,
                object_type=change.object_type, external_id=change.external_id, local_id=change.local_id,
                created_at=now, updated_at=now)
            tx.organization.mappings[link.id] = link
        tx.emit('membership_changed' if change.object_type == 'membership' else 'organization_changed',
                change.local_id, change.expected_version + 1, actor=who.actor, at=now)
