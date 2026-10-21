"""唯一触发扫描器仅受理WorkTask，实际执行仍归TaskRegistry。"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from .service import TaskDefinitionService

logger=logging.getLogger(__name__)


class TriggerWorker:
    """时间与逐行事件各自保存分页游标，不持有业务正文。"""

    def __init__(self,service: TaskDefinitionService) -> None:
        """生命周期由Platform持有，不创建第二个任务执行器。"""
        self.service=service
        self.time_cursor: tuple[str,str,str] | None=None
        self.event_cursor: tuple[str,str,str] | None=None
        self.job: asyncio.Task[None] | None=None
        self.accepting=False

    async def tick_time(self) -> None:
        """due索引每轮最多100定义，资格复查在本人事务中完成。"""
        if not self.service.config.enabled:
            return
        backend=self.service.workspace.store
        rows=await backend.trigger_candidates('time',after=self.time_cursor,limit=100,now=self.service.now())
        if not rows and self.time_cursor is not None:
            self.time_cursor=None
            rows=await backend.trigger_candidates('time',limit=100,now=self.service.now())
        for tenant,user,identifier in rows:
            self.time_cursor=tenant,user,identifier
            if not self.service.config.allows(tenant) or not self.service.tasks.durable.allows(tenant):
                continue
            who=Principal(role='tenant_member',tenant_id=tenant,user_id=user,key_id=None)
            try:
                await self.service.fire_time(who,identifier)
            except PermissionDeniedError:
                await self.service.revoke_unavailable(tenant,user)

    async def tick_events(self) -> None:
        """逐条未结算投递扫描，序号和时间戳均不充当提交水位。"""
        if not self.service.config.enabled:
            return
        backend=self.service.workspace.store
        rows=await backend.trigger_candidates('event',after=self.event_cursor,limit=100,now=self.service.now())
        if not rows and self.event_cursor is not None:
            self.event_cursor=None
            rows=await backend.trigger_candidates('event',limit=100,now=self.service.now())
        for tenant,user,identifier in rows:
            self.event_cursor=tenant,user,identifier
            if not self.service.config.allows(tenant) or not self.service.tasks.durable.allows(tenant):
                continue
            who=Principal(role='tenant_member',tenant_id=tenant,user_id=user,key_id=None)
            try:
                await self.service.deliver_event(who,identifier)
            except PermissionDeniedError:
                await self.service.revoke_unavailable(tenant,user)

    def start(self) -> None:
        """单次启动，开关关闭时不建立扫描任务。"""
        if self.job is not None or not self.service.config.enabled:
            return
        self.accepting=True
        self.job=asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        """关闭可取消等待，单项错误留持久事实供下一轮核查。"""
        while self.accepting:
            try:
                await self.tick_time()
                await self.tick_events()
            except Exception:
                logger.exception('触发扫描失败')
            await asyncio.sleep(self.service.config.scan_seconds)

    async def close(self) -> None:
        """先停止新受理并等待当前扫描退出，再交还任务执行生命周期。"""
        self.accepting=False
        if self.job is not None:
            self.job.cancel()
            await asyncio.gather(self.job,return_exceptions=True)
            self.job=None
