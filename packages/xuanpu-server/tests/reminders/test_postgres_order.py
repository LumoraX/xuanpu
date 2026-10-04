"""真实PostgreSQL事务提交顺序与通知游标的一致性验证。"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from xuanpu_server.audit import AuditLog
from xuanpu_server.controlplane.postgres import PostgresControlPlane
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.reminders.service import ReminderService
from xuanpu_server.tenancy import Principal, Tenant


async def test_same_employee_notification_commit_order(identity_stores, monkeypatch):
    """第一个事务拿到通知编号后暂停，第二事务不能越过它提前提交。"""
    control, identity = identity_stores
    if not isinstance(control, PostgresControlPlane):
        pytest.skip("本用例验证真实PostgreSQL行锁与事务")
    await control.create_tenant(Tenant(tenant_id="acme", name="Acme", created_at=1))
    await identity.create_user(
        UserRecord(tenant_id="acme", user_id="usr_a", display_name="甲", created_at=1)
    )
    service = ReminderService(
        store=control.reminder_store,
        control=control,
        identity=identity,
        audit=AuditLog(control),
        now=lambda: 1000,
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    payload = ReminderInput(title="提醒", due_at=1100.0, timezone="UTC")
    first = await service.create(who, payload, "first")
    second = await service.create(who, payload, "second")
    inserted, release = asyncio.Event(), asyncio.Event()
    original = asyncpg.Connection.fetchrow
    paused = False

    async def hold_after_insert(connection, query, *args, **kwargs):
        """执行真实INSERT后，仅首个通知事务等待测试放行。"""
        nonlocal paused
        result = await original(connection, query, *args, **kwargs)
        if "INSERT INTO" in query and '"xp_notification"' in query and not paused:
            paused = True
            inserted.set()
            await release.wait()
        return result

    monkeypatch.setattr(asyncpg.Connection, "fetchrow", hold_after_insert)
    task_a = asyncio.create_task(service.store.fire(first, 1200))
    task_b = None
    try:
        await asyncio.wait_for(inserted.wait(), 3)
        task_b = asyncio.create_task(service.store.fire(second, 1200))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), 0.1)
        # 未提交的较小编号不可见；同一员工也没有较大编号能越过它。
        assert await service.notifications(who) == []
        release.set()
        notifications = await asyncio.gather(task_a, task_b)
        assert notifications[0].notification_id < notifications[1].notification_id
        assert await service.notifications(who, after_id=notifications[0].notification_id) == [
            notifications[1]
        ]
    finally:
        release.set()
        await asyncio.gather(
            *[task for task in (task_a, task_b) if task is not None], return_exceptions=True
        )
