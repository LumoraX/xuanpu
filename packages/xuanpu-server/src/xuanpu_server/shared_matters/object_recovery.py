"""既有单实例持有权内恢复共享发布，不增加模型工作进程。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_server.shared_matters.materials import SharedMaterials

logger = logging.getLogger(__name__)


class PublicationRecovery:
    """逐条处理已过租期的意图，失败的对象清理可以独立重试。"""
    def __init__(self, materials: SharedMaterials) -> None:
        """共享服务使用控制面已有的租户列表及实例生命周期。"""
        self.materials = materials
        self._job: asyncio.Task[None] | None = None
        self._gc_after: dict[str, str] = {}

    async def recover(self, tenant: str) -> int:
        """有界选取待恢复意图，不依赖进程内任务是否存在。"""
        service = self.materials.service
        async with service.store.transaction(tenant) as tx:
            pending = sorted((p for p in tx.shared.publications.values() if
                p.status == 'pending' and p.lease_until <= service.now()), key=lambda p: p.id)[:100]
            tombstones = sorted((p for p in tx.shared.publications.values()
                if p.status in ('failed', 'withdrawn')), key=lambda p: p.id)
            after = self._gc_after.get(tenant, '')
            garbage = [p for p in tombstones if p.id > after][:100] or tombstones[:100]
            if garbage:
                self._gc_after[tenant] = garbage[-1].id
        for publication in pending:
            await self.materials.publications.execute(tenant, publication.id)
        for publication in garbage:
            await self.materials.publications.cleanup(tenant, publication.id)
        return len(pending) + len(garbage)

    async def recover_all(self) -> None:
        """租户不可用时不读取其内容或偷偷推进业务。"""
        for tenant in await self.materials.service.organization.control.list_tenants():
            if tenant.status == 'active':
                await self.recover(tenant.tenant_id)

    def start(self) -> None:
        """调用者已取得实例持有权，重复启动不创建并行扫描器。"""
        if self._job is None:
            self._job = asyncio.create_task(self._run(), name='shared-publication-recovery')

    async def _run(self) -> None:
        """异常只记录类型，不把私有材料或供应商凭据写入日志。"""
        while True:
            await asyncio.sleep(10)
            try:
                await self.recover_all()
            except Exception as exc:
                logger.warning('共享发布恢复暂未完成: %s', type(exc).__name__)

    async def close(self) -> None:
        """在对象仓储关闭和实例持有权释放前取消并等待恢复任务。"""
        if self._job is not None:
            self._job.cancel()
            with suppress(asyncio.CancelledError):
                await self._job
            self._job = None
