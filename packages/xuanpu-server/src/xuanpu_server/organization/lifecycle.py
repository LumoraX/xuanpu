"""持久治理事件收尾；即时授权依赖当前身份版本，绝不等待消费者。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from xuanpu_server.organization.store import OrganizationRepository

if TYPE_CHECKING:
    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.coordination.outbox import CoordinationEvent
    from xuanpu_server.organization.service import OrganizationService

logger = logging.getLogger(__name__)


class OrganizationLifecycle:
    """在既有单实例持有权内恢复事件，消费者逐条确认且可重复执行。"""
    def __init__(self, organization: OrganizationService) -> None:
        """复用控制面、协调事务和当前身份，没有独立身份缓存。"""
        self.org = organization
        self._job: asyncio.Task[None] | None = None

    async def recover(self, tenant: str) -> int:
        """最多处理100条已持久事件，身份停用事务已在进入前提交。"""
        async with self.org.store.transaction(tenant) as read:
            events = read.pending_events('organization')
        if not events:
            return 0
        users = tuple(str(e.data['user_id']) for e in events if e.kind == 'user_disabled')
        async with self.org.store.transaction(tenant, participants=users, allow_inactive=users) as tx:
            pending = {e.id for e in tx.pending_events('organization')}
            for event in events:
                if event.id not in pending:
                    continue
                if event.kind == 'user_disabled':
                    self._disabled(tx, event)
                tx.ack('organization', event.id)
        return len(events)

    def _disabled(self, tx: CoordinationTransaction, event: CoordinationEvent) -> None:
        """只撤销旧身份代次的职责；重新授权的新代次不被乱序回调撤销。"""
        user = str(event.data['user_id'])
        epoch = event.entity_version
        repository = OrganizationRepository(tx)
        now = self.org.now()
        for role in list(tx.organization.roles.values()):
            if role.user_id == user and role.user_epoch < epoch and role.status == 'active':
                updated = role.model_copy(update={'status': 'revoked', 'epoch': role.epoch + 1,
                                                  'version': role.version + 1, 'updated_at': now})
                repository.put_role(updated)
                tx.emit('role_revoked', role.id, updated.version, actor='system:organization', at=now,
                        data={'user_id': user, 'before': role.model_dump(mode='json'), 'after': updated.model_dump(mode='json')})
        for member in list(tx.organization.memberships.values()):
            if member.user_id == user and member.user_epoch < epoch and member.status == 'active':
                updated_member = member.model_copy(update={'status': 'revoked', 'epoch': member.epoch + 1,
                                                           'version': member.version + 1, 'updated_at': now})
                repository.put_membership(updated_member)
                tx.emit('membership_changed', member.id, updated_member.version, actor='system:organization', at=now,
                        data={'user_id': user, 'before': member.model_dump(mode='json'), 'after': updated_member.model_dump(mode='json')})

    async def recover_all(self) -> None:
        """重启扫描不依赖进程内回调，也不把最大事件序号当消费水位。"""
        for tenant in await self.org.control.list_tenants():
            if tenant.status == 'active':
                await self.recover(tenant.tenant_id)

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """监听者仅加速收尾；崩溃漏通知由持久扫描恢复。"""
        await self.recover(tenant_id)

    def start(self) -> None:
        """只能由平台获得既有实例持有权后启动。"""
        if self._job is None:
            self._job = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        """失败保留未确认事件，记录诊断后下一轮重试。"""
        while True:
            await asyncio.sleep(10)
            try:
                await self.recover_all()
            except Exception:
                logger.exception('组织治理事件收尾失败，未确认事件将重试')

    async def close(self) -> None:
        """释放实例持有权前取消扫描并等当前事务回滚。"""
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None
