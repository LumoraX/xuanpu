"""未读通知统计（R5-E0 概览用）：内存与 PostgreSQL 一致，按员工隔离，读后减少。"""

from __future__ import annotations

from typing import Any

from server.core.audit import AuditLog
from server.core.tenancy import Principal, Tenant
from server.domains.identity.models import UserRecord
from server.domains.reminders.models import ReminderInput
from server.domains.reminders.service import ReminderService


async def test_unread_count_and_latest(identity_stores: Any) -> None:
    """总数覆盖全部未读；摘要新的在前；他人不计；标读后减少。"""
    control, identity = identity_stores
    await control.create_tenant(Tenant(tenant_id="acme", name="Acme", created_at=1))
    for user in ("usr_a", "usr_b"):
        await identity.create_user(UserRecord(tenant_id="acme", user_id=user,
                                              display_name=user, created_at=1))
    service = ReminderService(store=control.reminder_store, control=control, identity=identity,
                              audit=AuditLog(control), now=lambda: 1000)
    alice = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    bob = Principal(role="tenant_member", tenant_id="acme", user_id="usr_b", key_id=None)
    fired = []
    for i in range(4):
        record = await service.create(alice, ReminderInput(title=f"提醒{i}", due_at=1100.0,
                                                           timezone="UTC"), f"r{i}")
        notification = await service.store.fire(record, 1200)
        assert notification is not None
        fired.append(notification)
    record = await service.create(bob, ReminderInput(title="Bob", due_at=1100.0, timezone="UTC"), "b")
    await service.store.fire(record, 1200)
    total, latest = await service.store.unread_notifications("acme", "usr_a", 2)
    assert total == 4
    assert [n.title for n in latest] == ["提醒3", "提醒2"]
    await service.read(alice, fired[3].notification_id)
    total, latest = await service.store.unread_notifications("acme", "usr_a", 5)
    assert total == 3 and latest[0].title == "提醒2"
    assert (await service.store.unread_notifications("acme", "usr_b", 5))[0] == 1
