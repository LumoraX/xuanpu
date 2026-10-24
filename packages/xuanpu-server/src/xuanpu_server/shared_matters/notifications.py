"""来源拥有的站内动态与统一通知入口共用当前ACL，不保存私有标题正文。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.shared_matters.models import SharedNotification
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.source_dependencies import authority_digest
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.models import SharedEvent
    from xuanpu_server.shared_matters.schemas import SharedChange
    from xuanpu_server.shared_matters.service import SharedMatterService

logger = logging.getLogger(__name__)
CONSUMER = 'shared-notifications'


class SharedNotifications:
    """有界逐事件/逐员工确认，序号在租户事务锁内分配，不采用来源ID最大水位。"""
    def __init__(self, service: SharedMatterService) -> None:
        """已有共享通知表就是核心收件箱，不再复制到员工提醒数据库。"""
        self.service = service
        self._job: asyncio.Task[None] | None = None

    @staticmethod
    def candidates(tx: CoordinationTransaction, event: SharedEvent) -> list[str]:
        """直接员工及明确部门当前任职是候选，实际读取资格在锁内再次检查。"""
        found = set()
        for grant in tx.shared.grants.values():
            if grant.matter_id != event.matter_id or grant.status != 'active' or grant.created_at > event.created_at:
                continue
            if grant.subject_kind == 'user':
                found.add(grant.subject_id)
            else:
                found.update(m.user_id for m in tx.organization.memberships.values()
                    if m.unit_id == grant.subject_id and m.status == 'active' and m.created_at <= event.created_at)
        return sorted(found - {event.actor_id})

    async def recover(self, tenant: str) -> int:
        """每批最多100名收件人，先落通知再逐条确认，崩溃重放不会重复。"""
        async with self.service.store.transaction(tenant) as tx:
            pending = tx.pending_events(CONSUMER)
            if not pending:
                return 0
            selected = None
            for event in pending:
                if event.kind.startswith('shared.'):
                    selected = event
                    break
                tx.ack(CONSUMER, event.id)
            if selected is None:
                return len(pending)
            source = tx.shared.history.get(str(selected.data.get('shared_event_id', '')))
            if source is None:
                raise ConflictError('共享通知来源缺失，不能确认消费')
            candidates = self.candidates(tx, source)
            users = tuple(user for user in candidates
                if selected.id not in tx.state.acknowledgements.get(CONSUMER + '/' + user, set()))[:100]
        return await self._deliver(tenant, selected.id, source.id, users)

    async def _deliver(self, tenant: str, event_id: str, source_id: str, users: tuple[str, ...]) -> int:
        """候选员工行锁内再次检查来源，通知和确认共用同一次提交。"""
        async with self.service.store.transaction(tenant, participants=users, allow_inactive=users) as tx:
            if event_id in tx.state.acknowledgements.get(CONSUMER, set()):
                return 1
            source = tx.shared.history[source_id]
            sequence = max((row.sequence for row in tx.shared.notifications.values()), default=0)
            for user in users:
                sequence = await self._recipient(tx, event_id, source, user, sequence)
            current_candidates = self.candidates(tx, source)
            if all(event_id in tx.state.acknowledgements.get(CONSUMER + '/' + user, set()) for user in current_candidates):
                tx.ack(CONSUMER, event_id)
        return len(users) or 1

    async def _recipient(self, tx: CoordinationTransaction, event_id: str, source: SharedEvent, user: str, sequence: int) -> int:
        """当前无权或停用的收件人不产生可见通知，同一来源同一员工只保存一次。"""
        if event_id in tx.state.acknowledgements.get(CONSUMER + '/' + user, set()):
            return sequence
        employee = await self.service.identity.get_user(tx.tenant_id, user)
        who = Principal(tenant_id=tx.tenant_id, user_id=user, role='tenant_member', key_id=None)
        try:
            access(tx, who, source.matter_id, self.service.now())
            if employee is None or employee.status != 'active':
                raise NotFoundError('收件人不可用')
        except NotFoundError:
            tx.ack(CONSUMER + '/' + user, event_id)
            return sequence
        semantic = source.id + ':' + user
        identifier = 'snt_' + hashlib.sha256((tx.tenant_id + ':' + semantic).encode()).hexdigest()[:32]
        if identifier not in tx.shared.notifications:
            sequence += 1
            now = self.service.now()
            tx.shared.notifications[identifier] = SharedNotification(tenant_id=tx.tenant_id, id=identifier,
                user_id=user, user_epoch=employee.lifecycle_epoch, sequence=sequence,
                matter_id=source.matter_id, event_id=source.id, semantic_key=semantic, created_at=now, updated_at=now)
        tx.ack(CONSUMER + '/' + user, event_id)
        return sequence

    def visible(self, tx: CoordinationTransaction, who: Principal, identifier: str) -> SharedNotification:
        """自己的旧通知也不是永久授权，停用再启用不会恢复旧身份收件箱。"""
        record = tx.shared.notifications.get(identifier)
        if record is None or record.user_id != who.user_id or record.user_epoch != tx.participant_epochs.get(record.user_id):
            raise NotFoundError('共享动态不存在')
        access(tx, who, record.matter_id, self.service.now())
        return record

    @staticmethod
    def view(record: SharedNotification) -> dict[str, JsonValue]:
        """只投影通用提示及来源引用，禁止旧通知留下私有标题或正文。"""
        return {key: value for key, value in record.model_dump(mode='json').items()
            if key in ('id', 'version', 'sequence', 'created_at', 'read_at')} | {
            'source': 'shared_matter', 'shared_id': record.matter_id,
            'title': '共享事项有新动态', 'body': '请打开查看当前可见的资料与进展。'}

    async def page(self, who: Principal, *, after_id: int = 0, before_id: int | None = None,
                   limit: int = 100, latest: bool = False) -> dict[str, JsonValue]:
        """先核ACL再计算独立总数和未读数，随后才进行顺序分页。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        if not 1 <= limit <= 100 or not 0 <= after_id <= 9223372036854775807 or (before_id is not None and not 1 <= before_id <= 9223372036854775807):
            raise InvalidRequestError('共享动态分页参数无效')
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            visible = []
            for record in tx.shared.notifications.values():
                try:
                    self.visible(tx, who, record.id)
                except NotFoundError:
                    continue
                visible.append(record)
            remaining = sorted((row for row in visible if row.sequence > after_id
                and (before_id is None or row.sequence < before_id)), key=lambda row: (row.sequence, row.id))
            selected = remaining[-limit:] if latest else remaining[:limit]
            footprints = []
            for identifier in sorted(tx.shared.matters):
                try:
                    matter, grants = access(tx, who, identifier, self.service.now())
                except NotFoundError:
                    continue
                footprints.append((matter.id, matter.access_epoch, sorted((g.id, g.version, g.epoch,
                    authority_digest(tx, who.user_id, g, self.service.now())) for g in grants)))
            stamp = repr((sorted((row.id, row.version, row.read_at) for row in visible), footprints))
            return {'items': [self.view(row) for row in selected], 'total': len(visible),
                'unread_total': sum(row.read_at is None for row in visible), 'has_more': len(remaining) > limit,
                'visibility_revision': hashlib.sha256(stamp.encode()).hexdigest(),
                'permission_revision': hashlib.sha256(repr(footprints).encode()).hexdigest(),
                'next_after_id': selected[-1].sequence if selected else after_id,
                'next_before_id': selected[0].sequence if selected and len(remaining) > limit else None}

    async def get(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """外发SSE或导航前再次补读当前来源资格。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            return self.view(self.visible(tx, who, identifier))

    async def read(self, who: Principal, identifier: str, body: SharedChange) -> dict[str, JsonValue]:
        """明确标读有稳定操作号及CAS；撤权后旧回执也不能绕过当前资格。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.notification.read', 'id': identifier, 'body': body.model_dump(mode='json')}
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            record = self.visible(tx, who, identifier)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            if record.version != body.expected_version:
                raise ConflictError('共享动态已变化，请刷新')
            if record.read_at is None:
                record = record.model_copy(update={'read_at': self.service.now(), 'updated_at': self.service.now(), 'version': record.version + 1})
                tx.shared.notifications[identifier] = record
            result: dict[str, JsonValue] = {'id': record.id, 'version': record.version, 'read_at': record.read_at}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def recover_all(self) -> None:
        """旧灰度关闭后也继续收尾已提交事件，不发外部消息。"""
        for tenant in await self.service.organization.control.list_tenants():
            if tenant.status == 'active':
                await self.recover(tenant.tenant_id)

    def start(self) -> None:
        """由已有单实例持有权启动同进程收尾，不新增模型工作者。"""
        if self._job is None:
            self._job = asyncio.create_task(self._run(), name='shared-inbox-recovery')

    async def _run(self) -> None:
        """失败保留未确认事件，日志不记录资料或身份凭据。"""
        while True:
            await asyncio.sleep(2)
            try:
                await self.recover_all()
            except Exception as exc:
                logger.warning('共享动态收尾暂未完成: %s', type(exc).__name__)

    async def close(self) -> None:
        """释放单实例持有权前取消并等待当前短事务退出。"""
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None
