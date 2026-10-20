"""独立提醒存储协议；所有写入按租户和员工限定。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import builtins

    from xuanpu_server.reminders.models import DeliveryRecord, NotificationRecord, ReminderRecord


class ReminderStore(Protocol):
    """原子提醒、通知和投递存储，由控制面提供同后端实例。"""

    async def create(
        self,
        record: ReminderRecord,
        now: float,
        *,
        actor: str | None = None,
        proposal_id: str | None = None,
    ) -> ReminderRecord:
        """幂等优先，在员工锁内校验身份、时间和限额。"""
        ...

    async def get_request(
        self, tenant_id: str, user_id: str, request_id: str
    ) -> ReminderRecord | None:
        """按本人原始创建请求查幂等记录。"""
        ...

    async def get(self, tenant_id: str, user_id: str, reminder_id: str) -> ReminderRecord:
        """只取本人的提醒；没有则404。"""
        ...

    async def list(
        self, tenant_id: str, user_id: str, *, status: str | None = None
    ) -> builtins.list[ReminderRecord]:
        """最近一百条提醒。"""
        ...

    async def update(
        self, record: ReminderRecord, version: int, now: float, *, actor: str | None = None
    ) -> ReminderRecord:
        """按版本编辑活动提醒。"""
        ...

    async def cancel(
        self,
        tenant_id: str,
        user_id: str,
        reminder_id: str,
        version: int,
        now: float,
        *,
        actor: str | None = None,
    ) -> ReminderRecord:
        """按版本取消；重复取消幂等。"""
        ...

    async def due(self, now: float) -> builtins.list[ReminderRecord]:
        """全局最多一百条到期候选，只供后台扫描。"""
        ...

    async def fire(self, record: ReminderRecord, now: float) -> NotificationRecord | None:
        """事务内重查身份版本，产生通知、投递任务并推进提醒。"""
        ...

    async def notifications(
        self, tenant_id: str, user_id: str, after_id: int, limit: int
    ) -> builtins.list[NotificationRecord]:
        """按递增持久游标读取本人的通知。"""
        ...

    async def notification_current(self, record: NotificationRecord) -> bool:
        """核实本人通知来源是否仍允许浮层提示和外部发送，不删除历史。"""
        ...

    async def read(
        self, tenant_id: str, user_id: str, notification_id: int, now: float
    ) -> NotificationRecord:
        """幂等标记通知已读。"""
        ...

    async def claim_deliveries(
        self, now: float
    ) -> builtins.list[tuple[DeliveryRecord, NotificationRecord]]:
        """复查身份后认领最多一百条投递，每次换令牌。"""
        ...

    async def deliveries(
        self, tenant_id: str, user_id: str, notification_id: int
    ) -> builtins.list[DeliveryRecord]:
        """列出本人通知的外部投递状态。"""
        ...

    async def finish_delivery(
        self, record: DeliveryRecord, status: str, now: float, error_code: str | None = None
    ) -> None:
        """仅当前令牌能回写，pending安排有限退避。"""
        ...
