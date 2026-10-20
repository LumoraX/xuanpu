"""业务处理与免打扰排队通知失效的双后端契约。"""

from __future__ import annotations

from dataclasses import asdict, replace

import pytest
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.reminders.models import NotificationRecord, ReminderRecord
from xuanpu_server.reminders.worker import ReminderWorker
from xuanpu_server.tenancy import Principal, Tenant
from xuanpu_server.workspace.service import WorkspaceService


async def guard():
    """测试身份已经建立，无额外条件。"""


@pytest.fixture
async def env(identity_stores, tmp_path):
    """创建本人工作区与独立员工，验证来源操作不越过员工边界。"""
    control, identity = identity_stores
    for tenant in ("acme", "other"):
        await control.create_tenant(Tenant(tenant_id=tenant, name=tenant, created_at=1.0))
        for user in ("usr_a", "usr_b"):
            await identity.create_user(
                UserRecord(tenant_id=tenant, user_id=user, display_name=user, created_at=1.0)
            )
    service = WorkspaceService(
        store=control.workspace_store,
        control=control,
        identity=identity,
        objects=LocalObjectStore(tmp_path / "objects"),
        now=lambda: 1000.0,
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    await service.update_settings(who, {"proactive_channels": ["inbox", "feishu"]}, 1)
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    return control, identity, service, item


async def source_notification(env, source, *, created_at=2000.0):
    """写入一个有真实来源、稍后投递的通知。"""
    control, _, service, item = env
    async with service.store.transaction("acme", "usr_a", guard) as state:
        record = {
            "id": "source-1",
            "item_id": item["id"],
            "generation": 1,
            "check_type": "stale",
            "stage_index": 0,
            "status": "notified",
            "last_notified_at": 2000.0,
        }
        records = state.checks if source == "proactive_check" else state.followups
        records["source-1"] = record
        state.pending_notifications.append(
            {
                "notification_key": "source-1:v1",
                "source": source,
                "proactive_check_id": "source-1" if source == "proactive_check" else None,
                "proactive_followup_id": "source-1" if source == "proactive_followup" else None,
                "title": "核实进展",
                "body": "报价发出了吗？",
                "due_at": None,
                "created_at": created_at,
                "alert_after": 2500.0,
                "channels": ["inbox", "feishu"],
            }
        )
    return (await control.reminder_store.notifications("acme", "usr_a", 0, 100))[0]


@pytest.mark.parametrize("source", ["proactive_check", "proactive_followup"])
@pytest.mark.parametrize("status", ["resolved", "ignored", "dismissed", "waiting"])
@pytest.mark.parametrize("claimed", [False, True])
async def test_handled_source_atomically_skips_old_deliveries(env, source, status, claimed):
    """处理来源立即撤销待投递或已认领任务，站内历史保留。"""
    control, _, service, _ = env
    notification = await source_notification(env, source)
    assert await control.reminder_store.notification_current(notification) is True
    if claimed:
        assert len(await control.reminder_store.claim_deliveries(2500.0)) == 1
    async with service.store.transaction("acme", "usr_a", guard) as state:
        records = state.checks if source == "proactive_check" else state.followups
        records["source-1"].update(status=status, next_review_at=3000.0, updated_at=2400.0)
    assert await control.reminder_store.notification_current(notification) is False
    delivery = (
        await control.reminder_store.deliveries("acme", "usr_a", notification.notification_id)
    )[0]
    assert delivery.status == "skipped" and delivery.error_code == "source_resolved"
    assert delivery.claim_token is None and delivery.lease_until == 0
    assert await control.reminder_store.claim_deliveries(3000.0) == []
    assert await control.reminder_store.notifications("acme", "usr_a", 0, 100) == [notification]


@pytest.mark.parametrize("claimed", [False, True])
async def test_cancel_completed_linked_reminder_skips_its_generated_delivery(env, claimed):
    """原生提醒已到账变为 completed 后，取消跟进仍撤销延后的外部投递。"""
    control, _, service, _ = env
    record = ReminderRecord(
        "rem-linked",
        "acme",
        "usr_a",
        "报价",
        1500.0,
        "UTC",
        "once",
        ("inbox", "feishu"),
        "active",
        1,
        "linked",
        1000.0,
        1000.0,
        "fp",
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_reminders.append(asdict(record))
        state.followups["follow-linked"] = {
            "id": "follow-linked",
            "status": "waiting",
            "reminder_id": record.reminder_id,
            "next_review_at": 1500.0,
        }
    notification = await control.reminder_store.fire(record, 2000.0)
    assert notification is not None and await control.reminder_store.notification_current(
        notification
    )
    assert (
        await control.reminder_store.get("acme", "usr_a", record.reminder_id)
    ).status == "completed"
    if claimed:
        assert len(await control.reminder_store.claim_deliveries(2000.0)) == 1
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-linked"]["status"] = "resolved"
        state.pending_reminder_cancellations.append(record.reminder_id)
    assert await control.reminder_store.notification_current(notification) is False
    delivery = (
        await control.reminder_store.deliveries("acme", "usr_a", notification.notification_id)
    )[0]
    assert delivery.status == "skipped" and delivery.error_code == "source_resolved"
    assert await control.reminder_store.claim_deliveries(2500.0) == []
    assert len(await control.reminder_store.notifications("acme", "usr_a", 0, 100)) == 1


async def test_current_source_checks_notification_time_and_scope(env):
    """同来源新证据通知与其他员工不能复用旧通知，手工原生通知保持兼容。"""
    control, _, service, _ = env
    notification = await source_notification(env, "proactive_followup")
    assert (
        await control.reminder_store.notification_current(replace(notification, user_id="usr_b"))
        is False
    )
    assert (
        await control.reminder_store.notification_current(replace(notification, tenant_id="other"))
        is False
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["source-1"]["last_notified_at"] = 2400.0
    assert await control.reminder_store.notification_current(notification) is False
    manual = NotificationRecord(100, "acme", "usr_a", "manual", "手工提醒", 2000.0, 2000.0)
    assert await control.reminder_store.notification_current(manual) is True


async def test_claim_rejects_notification_with_outdated_source_snapshot(env):
    """旧通知快照仍可能入队，认领阶段必须独立核实并跳过。"""
    control, _, _, _ = env
    notification = await source_notification(env, "proactive_check", created_at=1900.0)
    assert await control.reminder_store.notification_current(notification) is False
    assert await control.reminder_store.claim_deliveries(2500.0) == []
    delivery = (
        await control.reminder_store.deliveries("acme", "usr_a", notification.notification_id)
    )[0]
    assert delivery.status == "skipped" and delivery.error_code == "source_resolved"


async def test_send_rechecks_source_after_delivery_was_claimed(env):
    """认领后用户已处理，发送器之前的复查阻止真实外部消息。"""
    control, identity, service, _ = env
    notification = await source_notification(env, "proactive_followup")
    delivery, _ = (await control.reminder_store.claim_deliveries(2500.0))[0]
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["source-1"]["status"] = "resolved"

    class Sender:
        """记录外部调用，避免测试真正发送。"""

        def __init__(self):
            """从零次调用开始。"""
            self.calls = 0

        async def send(self, notification, channel):
            """任何调用都应由测试观察到。"""
            self.calls += 1

    sender = Sender()
    worker = ReminderWorker(
        store=control.reminder_store,
        control=control,
        identity=identity,
        sender=sender,
        now=lambda: 2500.0,
    )
    await worker._send(delivery, notification)
    assert sender.calls == 0


async def test_native_reminder_requires_the_same_current_followup_time(env):
    """关联跟进已改期时，旧原生提醒不能再次浮层或发送。"""
    control, _, service, _ = env
    record = ReminderRecord(
        "rem-old-time",
        "acme",
        "usr_a",
        "报价",
        1500.0,
        "UTC",
        "once",
        ("inbox", "feishu"),
        "active",
        1,
        "old-time",
        1000.0,
        1000.0,
        "fp",
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_reminders.append(asdict(record))
        state.followups["follow-old-time"] = {
            "id": "follow-old-time",
            "status": "waiting",
            "reminder_id": record.reminder_id,
            "next_review_at": 1500.0,
        }
    notification = await control.reminder_store.fire(record, 2000.0)
    assert notification is not None
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-old-time"]["next_review_at"] = 3000.0
    assert await control.reminder_store.notification_current(notification) is False
    assert await control.reminder_store.claim_deliveries(2500.0) == []
    # 取消队列即便来源已不再保留关联，也会让原生通知持续失效。
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-old-time"].pop("reminder_id")
        state.pending_reminder_cancellations.append(record.reminder_id)
    assert await control.reminder_store.notification_current(notification) is False


async def test_delivery_revocation_rolls_back_with_source_audit(env, monkeypatch):
    """业务动作审计失败时，来源状态和待投递撤销必须整体回滚。"""
    control, _, service, _ = env
    notification = await source_notification(env, "proactive_followup")
    if hasattr(control, "_pool"):
        ns = control._namespace
        async with control._pool.acquire() as conn:
            await conn.execute(
                f'''CREATE FUNCTION "{ns.schema}".reject_revocation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'revocation_audit_rejected'; END; $$'''
            )
            await conn.execute(
                f'''CREATE TRIGGER reject_revocation BEFORE INSERT ON {ns.table("audit")} FOR EACH ROW EXECUTE FUNCTION "{ns.schema}".reject_revocation()'''
            )
    else:

        def reject(records):
            """同步审计阻止整个业务决定提交。"""
            if records:
                raise RuntimeError("revocation_audit_rejected")

        monkeypatch.setattr(service.store, "_audit", reject)
    with pytest.raises(Exception, match="revocation_audit_rejected"):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.followups["source-1"]["status"] = "resolved"
            state.audits.append(
                {
                    "actor": "user:usr_a",
                    "kind": "proactive.complete",
                    "at": 2400.0,
                    "data": {"id": "source-1"},
                }
            )
    assert await control.reminder_store.notification_current(notification) is True
    delivery = (
        await control.reminder_store.deliveries("acme", "usr_a", notification.notification_id)
    )[0]
    assert delivery.status == "pending" and delivery.error_code is None


async def test_handled_followup_old_notification_stays_inactive_when_review_resumes(env):
    """后续到期重新核实也不能复活用户已经处理过的旧卡片。"""
    control, _, service, _ = env
    notification = await source_notification(env, "proactive_followup")
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["source-1"].update(status="waiting", handled_at=2400.0)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["source-1"]["status"] = "awaiting_user"
    assert await control.reminder_store.notification_current(notification) is False


async def test_semantic_review_does_not_cancel_confirmed_native_delivery(env):
    """后台复查时间前移不等于用户改期，已经确认的提醒仍可投递。"""
    control, _, service, _ = env
    record = ReminderRecord(
        "review-rem",
        "acme",
        "usr_a",
        "报价",
        1500.0,
        "UTC",
        "once",
        ("inbox", "feishu"),
        "active",
        1,
        "review-rem",
        1000.0,
        1000.0,
        "fp",
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_reminders.append(asdict(record))
        state.followups["review-follow"] = {
            "id": "review-follow",
            "status": "waiting",
            "reminder_id": record.reminder_id,
            "due_at": 1500.0,
            "next_review_at": 1500.0,
        }
    notification = await control.reminder_store.fire(record, 2000.0)
    assert notification is not None
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["review-follow"].update(status="awaiting_user", next_review_at=90000.0)
    assert await control.reminder_store.notification_current(notification) is True


async def linked_reminder(env):
    """模拟主动安排的未来原生提醒和旧提示卡。"""
    control, _, service, _ = env
    record = ReminderRecord(
        "rem-edit",
        "acme",
        "usr_a",
        "报价",
        1500.0,
        "UTC",
        "once",
        ("inbox", "feishu"),
        "active",
        1,
        "edit",
        1000.0,
        1000.0,
        "fp",
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_reminders.append(asdict(record))
        state.followups["follow-edit"] = {
            "id": "follow-edit",
            "status": "waiting",
            "version": 1,
            "reminder_id": record.reminder_id,
            "due_at": 1500.0,
            "next_review_at": 1500.0,
        }
        state.pending_notifications.append(
            {
                "notification_key": "follow-edit:old",
                "source": "proactive_followup",
                "proactive_followup_id": "follow-edit",
                "title": "报价",
                "body": "核实进展",
                "due_at": 1500.0,
                "created_at": 1000.0,
                "alert_after": 3000.0,
                "channels": ["inbox", "feishu"],
            }
        )
    return await control.reminder_store.get("acme", "usr_a", record.reminder_id)


async def test_native_management_edit_synchronizes_proactive_followup(env):
    """提醒管理页改期同步跟进等待时间，新时间通知仍可投递。"""
    control, _, service, _ = env
    record = await linked_reminder(env)
    updated = await control.reminder_store.update(replace(record, due_at=4000.0), 1, 1200.0)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        followup = state.followups["follow-edit"]
        assert followup["due_at"] == 4000.0 and followup["next_review_at"] == 4000.0
        assert followup["status"] == "waiting" and followup["version"] == 2
        assert followup["handled_at"] == followup["updated_at"] == 1200.0
    old = (await control.reminder_store.notifications("acme", "usr_a", 0, 100))[0]
    old_delivery = (await control.reminder_store.deliveries("acme", "usr_a", old.notification_id))[
        0
    ]
    assert old_delivery.status == "skipped"
    assert await control.reminder_store.fire(updated, 1500.0) is None
    notification = await control.reminder_store.fire(updated, 4000.0)
    assert notification is not None and await control.reminder_store.notification_current(
        notification
    )
    assert len(await control.reminder_store.claim_deliveries(4000.0)) == 1


async def test_native_management_cancel_dismisses_linked_followup_only_for_owner(env):
    """取消入口停止对应主动跟进，其他员工和租户的关联记录保持原状态。"""
    control, _, service, _ = env
    record = await linked_reminder(env)
    for tenant, user in (("acme", "usr_b"), ("other", "usr_a")):
        async with service.store.transaction(tenant, user, guard) as state:
            state.followups["other-follow"] = {
                "id": "other-follow",
                "status": "waiting",
                "version": 1,
                "reminder_id": record.reminder_id,
                "next_review_at": 1500.0,
            }
    await control.reminder_store.cancel("acme", "usr_a", record.reminder_id, 1, 1200.0)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        followup = state.followups["follow-edit"]
        assert followup["status"] == "dismissed" and followup["version"] == 2
        assert followup["handled_at"] == 1200.0
    for tenant, user in (("acme", "usr_b"), ("other", "usr_a")):
        async with service.store.transaction(tenant, user, guard) as state:
            assert state.followups["other-follow"]["status"] == "waiting"
            assert state.followups["other-follow"]["version"] == 1
    old = (await control.reminder_store.notifications("acme", "usr_a", 0, 100))[0]
    assert (await control.reminder_store.deliveries("acme", "usr_a", old.notification_id))[
        0
    ].status == "skipped"
    assert await control.reminder_store.due(5000.0) == []


@pytest.mark.parametrize("cancelled", [False, True])
async def test_management_reminder_and_followup_roll_back_on_audit_failure(
    env, monkeypatch, cancelled
):
    """提醒管理操作审计失败时，提醒、跟进时间和旧投递资格一起回滚。"""
    control, _, service, _ = env
    record = await linked_reminder(env)
    if hasattr(control, "_pool"):
        ns = control._namespace
        async with control._pool.acquire() as conn:
            await conn.execute(
                f'''CREATE FUNCTION "{ns.schema}".reject_management() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'management_audit_rejected'; END; $$'''
            )
            await conn.execute(
                f'''CREATE TRIGGER reject_management BEFORE INSERT ON {ns.table("audit")} FOR EACH ROW EXECUTE FUNCTION "{ns.schema}".reject_management()'''
            )
    else:

        def reject(record):
            """拒绝同步提醒审计，不能留下关联工作区变更。"""
            raise RuntimeError("management_audit_rejected")

        monkeypatch.setattr(control, "_append_reminder_audit", reject)
    with pytest.raises(Exception, match="management_audit_rejected"):
        if cancelled:
            await control.reminder_store.cancel("acme", "usr_a", record.reminder_id, 1, 1200.0)
        else:
            await control.reminder_store.update(replace(record, due_at=4000.0), 1, 1200.0)
    assert await control.reminder_store.get("acme", "usr_a", record.reminder_id) == record
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.followups["follow-edit"]["due_at"] == 1500.0
        assert state.followups["follow-edit"]["version"] == 1
        assert state.followups["follow-edit"]["status"] == "waiting"
    old = (await control.reminder_store.notifications("acme", "usr_a", 0, 100))[0]
    assert (await control.reminder_store.deliveries("acme", "usr_a", old.notification_id))[
        0
    ].status == "pending"


async def test_native_management_edit_waits_for_workspace_snapshot_commit(env):
    """提醒管理与工作区共享员工锁，改期不能被较早打开的快照覆盖。"""
    import asyncio

    control, _, service, _ = env
    record = await linked_reminder(env)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_workspace():
        """保留真实员工事务，模拟并发业务状态提交。"""
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.followups["follow-edit"]["evidence_marker"] = "保留证据"
            entered.set()
            await release.wait()

    held = asyncio.create_task(hold_workspace())
    await asyncio.wait_for(entered.wait(), 3)
    updated = asyncio.create_task(
        control.reminder_store.update(replace(record, due_at=4000.0), 1, 1200.0)
    )
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(updated), 0.05)
    finally:
        release.set()
        await asyncio.gather(held, updated)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.followups["follow-edit"]["due_at"] == 4000.0
        assert state.followups["follow-edit"]["evidence_marker"] == "保留证据"
