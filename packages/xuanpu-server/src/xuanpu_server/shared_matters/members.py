"""明确邀请员工或可选部门，撤权及权属转交不能由模型自行决定。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.models import SharedAccessGrant, SharedMember
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.views import page

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.schemas import (
        SharedChange,
        SharedMemberInput,
        TransferOwnerInput,
    )
    from xuanpu_server.shared_matters.service import SharedMatterService
    from xuanpu_server.tenancy import Principal


class SharedMembers:
    """成员管理接受共享服务的同一个工作单元，不单独提交。"""
    def __init__(self, service: SharedMatterService) -> None:
        """固定领域依赖，员工选择不要求存在部门。"""
        self.service = service

    async def listing(self, who: Principal, identifier: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """成员列表只给当前整事项成员，限定资料成员无法枚举。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            access(tx, who, identifier, self.service.now(), full=True)
            return page([m.model_dump(mode='json') for m in tx.shared.members.values()
                         if m.matter_id == identifier], limit=limit, cursor=cursor)

    def revoke_record(self, tx: CoordinationTransaction, member: SharedMember) -> None:
        """保留成员和授权历史，递增权限代次，禁止原授权复活。"""
        if member.status == 'revoked':
            raise ConflictError('该授权已撤销')
        now = self.service.now()
        tx.shared.members[member.id] = member.model_copy(update={'status': 'revoked', 'version': member.version + 1, 'updated_at': now})
        grant = tx.shared.grants[member.grant_id]
        tx.shared.grants[grant.id] = grant.model_copy(update={'status': 'revoked', 'version': grant.version + 1,
            'epoch': grant.epoch + 1, 'updated_at': now})

    async def add(self, who: Principal, identifier: str, body: SharedMemberInput) -> dict[str, JsonValue]:
        """直接员工授权绑定身份代次，部门授权仅作为可选路径。"""
        service = self.service
        who = await service.actor(who)
        assert who.user_id is not None
        target = (body.subject_id,) if body.subject_kind == 'user' else ()
        payload: dict[str, JsonValue] = {'action': 'shared.invite', 'id': identifier, 'body': body.model_dump(mode='json')}
        async with service.store.transaction(who.require_tenant(), participants=(who.user_id, *target)) as tx:
            await service.actor(who)
            access(tx, who, identifier, service.now(), owner=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            service.enabled(tx.tenant_id)
            matter = await service.writable(tx, who, identifier, body.expected_version, owner=True)
            self._validate(tx, identifier, body)
            now, grant_id, member_id = service.now(), new_id('sgr'), new_id('smb')
            data = body.model_dump(exclude={'request_id', 'confirmed', 'expected_version', 'role'})
            try:
                grant = SharedAccessGrant(tenant_id=tx.tenant_id, id=grant_id, matter_id=identifier,
                    subject_epoch=tx.participant_epochs.get(body.subject_id) if target else None,
                    content_snapshot=matter.content if body.scope == 'packet' and body.include_content else None,
                    created_at=now, updated_at=now, **data)
            except ValidationError as exc:
                raise InvalidRequestError('授权范围不完整') from exc
            tx.shared.grants[grant_id] = grant
            tx.shared.members[member_id] = SharedMember(tenant_id=tx.tenant_id, id=member_id,
                matter_id=identifier, grant_id=grant_id, subject_kind=body.subject_kind,
                subject_id=body.subject_id, role=body.role, created_at=now, updated_at=now)
            matter = service.bump(tx, matter, now=now)
            service.event(tx, who, matter, 'member_added', {'member_id': member_id, 'grant_id': grant_id})
            result: dict[str, JsonValue] = {'id': member_id, 'grant_id': grant_id, 'version': matter.version}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    def _validate(self, tx: CoordinationTransaction, identifier: str, body: SharedMemberInput) -> None:
        """禁止空部门、重复活授权、扩大packet及无效版本集合。"""
        now = self.service.now()
        if body.expires_at is not None and body.expires_at <= now:
            raise InvalidRequestError('授权期限必须晚于当前时间')
        if body.scope == 'packet' and body.role != 'viewer':
            raise InvalidRequestError('限定材料授权仅允许只读')
        if body.scope == 'full' and (body.material_revisions or body.packet_title or body.include_content):
            raise InvalidRequestError('整事项与限定材料参数不能混用')
        if body.subject_kind == 'unit':
            unit = tx.organization.units.get(body.subject_id)
            if unit is None or unit.status != 'active':
                raise InvalidRequestError('授权部门不可用')
        for revision_id in body.material_revisions:
            revision = tx.shared.revisions.get(revision_id)
            if revision is None or revision.matter_id != identifier or revision.status != 'ready':
                raise InvalidRequestError('选定材料版本不可用')
        for member in tx.shared.members.values():
            if (member.matter_id, member.subject_kind, member.subject_id, member.status) != (identifier, body.subject_kind, body.subject_id, 'active'):
                continue
            grant = tx.shared.grants[member.grant_id]
            epoch = tx.participant_epochs.get(body.subject_id)
            if grant.status == 'active' and (grant.expires_at is None or grant.expires_at > now) and (
                    body.subject_kind == 'unit' or grant.subject_epoch == epoch):
                raise ConflictError('已有该参与者的有效授权，请先明确撤销')

    async def revoke(self, who: Principal, identifier: str, member_id: str, body: SharedChange) -> dict[str, JsonValue]:
        """关闭灰度后仍能撤销，唯一owner必须走明确转交。"""
        service = self.service
        who = await service.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.revoke', 'id': identifier, 'member_id': member_id, 'body': body.model_dump(mode='json')}
        async with service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await service.actor(who)
            access(tx, who, identifier, service.now(), owner=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            matter = await service.writable(tx, who, identifier, body.expected_version, owner=True)
            member = tx.shared.members.get(member_id)
            if member is None or member.matter_id != identifier:
                raise NotFoundError('共享成员不存在')
            if member.role == 'owner':
                raise ConflictError('唯一负责人须明确转交，不能直接撤销')
            self.revoke_record(tx, member)
            matter = service.bump(tx, matter, now=service.now(), invalidate=True)
            service.event(tx, who, matter, 'member_revoked', {'member_id': member_id})
            result: dict[str, JsonValue] = {'id': member_id, 'version': matter.version, 'status': 'revoked'}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def transfer(self, who: Principal, identifier: str, body: TransferOwnerInput) -> dict[str, JsonValue]:
        """原负责人明确转交给启用员工；事务内保持唯一owner及历史。"""
        service = self.service
        who = await service.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.transfer', 'id': identifier, 'body': body.model_dump(mode='json')}
        async with service.store.transaction(who.require_tenant(), participants=(who.user_id, body.user_id)) as tx:
            await service.actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            service.enabled(tx.tenant_id)
            matter = await service.writable(tx, who, identifier, body.expected_version, owner=True)
            if matter.owner_id == body.user_id:
                raise InvalidRequestError('新负责人须是另一名员工')
            for member in list(tx.shared.members.values()):
                if member.matter_id == identifier and member.status == 'active' and member.subject_kind == 'user' and member.subject_id in (who.user_id, body.user_id):
                    self.revoke_record(tx, member)
            matter = service.bump(tx, matter, now=service.now(), changes={'owner_id': body.user_id}, invalidate=True)
            service.owner(tx, matter, body.user_id)
            service.event(tx, who, matter, 'owner_transferred', {'from_user_id': who.user_id, 'to_user_id': body.user_id})
            result: dict[str, JsonValue] = {'id': identifier, 'version': matter.version, 'status': matter.status}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result
