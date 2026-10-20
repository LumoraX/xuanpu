"""主动记录、统一通知及待办代际的双后端契约。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError
from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.tenancy import Principal, Tenant
from xuanpu_server.workspace.models import SettingsData
from xuanpu_server.workspace.service import WorkspaceService


async def guard():
    """测试已创建的活动员工。"""


@pytest.fixture
async def env(identity_stores, tmp_path):
    """提供两个租户、同名员工和可推进的时钟。"""
    control, identity = identity_stores
    for tenant in ("acme", "other"):
        await control.create_tenant(Tenant(tenant_id=tenant, name=tenant, created_at=1.0))
        for user in ("usr_a", "usr_b"):
            await identity.create_user(
                UserRecord(tenant_id=tenant, user_id=user, display_name=user, created_at=1.0)
            )
    clock = [1000.0]
    service = WorkspaceService(
        store=control.workspace_store,
        control=control,
        identity=identity,
        objects=LocalObjectStore(tmp_path / "objects"),
        now=lambda: clock[0],
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    return control, identity, service, who, clock


def check(item):
    """生成规则检查的稳定代际键。"""
    return {
        "id": "check-1",
        "item_id": item["id"],
        "generation": 1,
        "check_type": "stale",
        "stage_index": 0,
        "status": "notified",
    }


def notification(**changes):
    """提供统一 outbox 的完整通知。"""
    return dict(
        {
            "notification_key": "check-1",
            "source": "proactive_check",
            "proactive_check_id": "check-1",
            "proactive_followup_id": None,
            "title": "待办核实",
            "body": "这件事完成了吗？",
            "due_at": None,
            "created_at": 2000.0,
            "alert_after": 2500.0,
            "channels": ["inbox"],
        },
        **changes,
    )


async def test_state_outbox_dedup_and_rollback(env):
    """主动状态与通知同事务提交，重复提交稳定键只留一个通知。"""
    control, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    store = service.store
    with pytest.raises(RuntimeError):
        async with store.transaction("acme", "usr_a", guard) as state:
            state.checks["check-1"] = check(item)
            state.pending_notifications.append(notification())
            raise RuntimeError("rollback")
    assert await control.reminder_store.notifications("acme", "usr_a", 0, 100) == []
    async with store.transaction("acme", "usr_a", guard) as state:
        assert state.checks == {}
        state.checks["check-1"] = check(item)
        state.followups["follow-1"] = {"id": "follow-1", "item_id": item["id"], "status": "waiting"}
        state.scan_state = {"cursor": {"message": 123}, "lease": [1, 2]}
        state.pending_notifications.append(notification())
    async with store.transaction("acme", "usr_a", guard) as state:
        assert state.scan_state == {"cursor": {"message": 123}, "lease": [1, 2]}
        assert state.followups["follow-1"]["status"] == "waiting"
        assert state.pending_notifications == []
        state.pending_notifications.append(notification())
    rows = await control.reminder_store.notifications("acme", "usr_a", 0, 100)
    assert len(rows) == 1 and rows[0].source == "proactive_check"
    assert rows[0].body == "这件事完成了吗？" and rows[0].due_at is None
    assert rows[0].alert_after == 2500.0
    assert await control.reminder_store.notifications("other", "usr_a", 0, 100) == []


async def test_owners_paging_and_restart(env):
    """已有工作区按租户员工稳定分页，重建数据库适配器仍能读。"""
    control, _, service, who, _ = env
    for owner in (who, replace(who, user_id="usr_b"), replace(who, tenant_id="other")):
        await service.create(owner, "todo", {"title": "任务"}, "todo")
    store = service.store
    assert await store.owners(limit=2) == [("acme", "usr_a"), ("acme", "usr_b")]
    assert await store.owners(after=("acme", "usr_b"), limit=2) == [("other", "usr_a")]
    assert await store.owners(after=("other", "usr_a")) == []
    if hasattr(control, "_pool"):
        store = type(store)(control._pool, namespace=control._namespace)
        async with store.transaction("acme", "usr_a", guard) as state:
            assert len(state.items) == 1


async def test_todo_progress_and_old_generation_closure(env):
    """标题不算进展，延期、完成和回收原子关闭关联跟进。"""
    _, _, service, who, clock = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    assert item["generation"] == 1 and item["last_progress_at"] == 1000.0
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.checks["check-1"] = check(item)
        state.followups["follow-1"] = {"id": "follow-1", "item_id": item["id"], "status": "waiting"}
    clock[0] = 2000.0
    item = await service.update(who, item["id"], {"title": "新标题"}, 1)
    assert item["generation"] == 1 and item["last_progress_at"] == 1000.0
    item = await service.update(who, item["id"], {"due_at": 3000.0}, 2)
    assert item["generation"] == 2 and item["last_progress_at"] == 2000.0
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.checks["check-1"]["status"] == "resolved"
        assert state.followups["follow-1"]["status"] == "resolved"
    with pytest.raises(InvalidRequestError):
        await service.update(who, item["id"], {"generation": 999}, 3)
    item = await service.update(who, item["id"], {"status": "done"}, 3)
    assert item["generation"] == 3
    clock[0] = 4000.0
    item = await service.update(who, item["id"], {"status": "open"}, 4)
    assert item["generation"] == 4 and item["last_progress_at"] == 4000.0
    item = await service.trash(who, item["id"], 5)
    assert item["generation"] == 5
    restored = await service.restore(who, item["id"], 6)
    assert restored["last_progress_at"] == 4000.0


async def test_sources_and_personal_channel_optin(env):
    """无来源、跨员工来源和未本人启用的外部渠道一律拒绝。"""
    control, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    with pytest.raises(Exception):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.pending_notifications.append(notification())
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.checks["check-1"] = check(item)
    with pytest.raises(Exception):
        async with service.store.transaction("other", "usr_a", guard) as state:
            state.pending_notifications.append(notification())
    with pytest.raises(InvalidRequestError):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.pending_notifications.append(notification(channels=["inbox", "feishu"]))
    await service.update_settings(who, {"proactive_channels": ["inbox", "feishu"]}, 1)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_notifications.append(notification(channels=["inbox", "feishu"]))
    assert await control.reminder_store.claim_deliveries(2499.0) == []
    found = await control.reminder_store.claim_deliveries(2500.0)
    assert len(found) == 1 and found[0][0].next_attempt_at == 2500.0


def test_settings_strict_nested_defaults():
    """主动设置向后兼容且严格校验时间、类型与渠道。"""
    settings = SettingsData().model_dump()
    assert settings["proactive_checks"]["enabled"] is True
    assert settings["proactive_channels"] == ["inbox"]
    for changes in (
        {"proactive_checks": {"enabled": "yes"}},
        {"proactive_checks": {"quiet_hours": {"start": "24:00"}}},
        {"proactive_checks": {"ignored_types": [{"type": "stale", "until": "later"}]}},
        {"proactive_channels": ["feishu"]},
    ):
        with pytest.raises(ValidationError):
            SettingsData.model_validate(changes)


async def test_sources_reject_foreign_items_and_duplicate_stages(env):
    """检查和语义跟进来源不能关联其他员工的待办，同代阶段唯一。"""
    _, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "本人"}, "todo")
    for table in ("checks", "followups"):
        with pytest.raises(Exception):
            async with service.store.transaction("acme", "usr_b", guard) as state:
                getattr(state, table)["foreign"] = check(item)
    with pytest.raises(Exception):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.checks["one"] = check(item)
            state.checks["two"] = {**check(item), "id": "two"}


async def test_followup_notifications_persist_and_rebuild(env):
    """无待办语义关注可持久通知，重建适配器读取全部正文与扫描状态。"""
    control, _, service, _, _ = env
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-1"] = {
            "id": "follow-1",
            "status": "awaiting_user",
            "source_message_id": 1,
        }
        state.scan_state = {"semantic": {"after_message_id": 1}}
        state.pending_notifications.append(
            notification(
                notification_key="follow-1:e1:s1",
                source="proactive_followup",
                proactive_check_id=None,
                proactive_followup_id="follow-1",
            )
        )
    store = service.store
    if hasattr(control, "_pool"):
        store = type(store)(control._pool, namespace=control._namespace)
    async with store.transaction("acme", "usr_a", guard) as state:
        assert state.followups["follow-1"]["source_message_id"] == 1
        assert state.scan_state["semantic"]["after_message_id"] == 1
    rows = await control.reminder_store.notifications("acme", "usr_a", 0, 100)
    assert rows[0].source == "proactive_followup" and rows[0].proactive_followup_id == "follow-1"


async def test_notification_lock_serializes_reminder_and_proactive(env):
    """工作区事务持有员工锁时提醒不能抢先提交通知。"""
    from xuanpu_server.reminders.models import ReminderRecord

    control, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "待办"}, "todo")
    reminder = ReminderRecord(
        "rem-1",
        "acme",
        "usr_a",
        "提醒",
        1100.0,
        "UTC",
        "once",
        ("inbox",),
        "active",
        1,
        "rem-1",
        1000.0,
        1000.0,
        "fp",
    )
    await control.reminder_store.create(reminder, 1000.0)
    entered, release = asyncio.Event(), asyncio.Event()

    async def proactive():
        """模拟检查保存前的短事务暂停。"""
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.checks["check-1"] = check(item)
            state.pending_notifications.append(notification())
            entered.set()
            await release.wait()

    first = asyncio.create_task(proactive())
    await asyncio.wait_for(entered.wait(), 3)
    second = asyncio.create_task(control.reminder_store.fire(reminder, 2000.0))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.05)
        assert await control.reminder_store.notifications("acme", "usr_a", 0, 100) == []
    finally:
        release.set()
        await asyncio.gather(first, second)
    rows = await control.reminder_store.notifications("acme", "usr_a", 0, 100)
    assert [row.source for row in rows] == ["proactive_check", "reminder"]
    assert rows[0].notification_id < rows[1].notification_id


async def test_disable_closes_records_and_skips_deliveries(env):
    """员工停用时关闭尚未处理关注，重新启用不能恢复旧状态。"""
    control, identity, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    await service.update_settings(who, {"proactive_channels": ["inbox", "feishu"]}, 1)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.checks["check-1"] = check(item)
        state.followups["follow-1"] = {"id": "follow-1", "item_id": item["id"], "status": "waiting"}
        state.pending_notifications.append(notification(channels=["inbox", "feishu"]))
    await identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=2300.0)
    await identity.set_user_status("acme", "usr_a", "active", at=2400.0)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.checks["check-1"]["status"] == "resolved"
        assert state.followups["follow-1"]["status"] == "resolved"
    rows = await control.reminder_store.notifications("acme", "usr_a", 0, 100)
    assert len(rows) == 1
    delivery = await control.reminder_store.deliveries("acme", "usr_a", rows[0].notification_id)
    assert delivery[0].status == "skipped"


async def test_legacy_progress_and_explicit_progress(env):
    """旧记录保留未知标记，显式确认进展才推进跟进目标。"""
    from xuanpu_server.workspace.rules import apply_todo_change

    _, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    async with service.store.transaction("acme", "usr_a", guard) as state:
        for key in ("generation", "last_progress_at", "progress_unknown"):
            del state.items[item["id"]][key]
    async with service.store.transaction("acme", "usr_a", guard) as state:
        loaded = state.items[item["id"]]
        assert loaded["last_progress_at"] == 1000.0 and loaded["progress_unknown"] is True
        apply_todo_change(state, loaded, {}, 2000.0, progress=True)
        assert loaded["last_progress_at"] == 2000.0 and loaded["generation"] == 2
        assert loaded["progress_unknown"] is False


async def test_prepared_reminder_and_followup_commit_atomically(env):
    """准备提醒不写库，关注决定与原生提醒共享一次提交和幂等身份。"""
    from dataclasses import asdict

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.reminders.models import ReminderInput
    from xuanpu_server.reminders.service import ReminderService

    control, identity, service, who, _ = env
    reminders = ReminderService(
        store=control.reminder_store,
        control=control,
        identity=identity,
        audit=AuditLog(control),
        now=lambda: 1000.0,
    )
    body = ReminderInput(title="报价提醒", due_at=3000.0, timezone="UTC")
    prepared = await reminders.prepare(who, body, "follow-reminder")
    assert await reminders.list(who) == []
    with pytest.raises(RuntimeError):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.followups["follow-1"] = {
                "id": "follow-1",
                "status": "waiting",
                "reminder_id": prepared.reminder_id,
            }
            state.pending_reminders.append(asdict(prepared))
            raise RuntimeError("rollback")
    assert await reminders.list(who) == []
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-1"] = {
            "id": "follow-1",
            "status": "waiting",
            "reminder_id": prepared.reminder_id,
        }
        state.pending_reminders.append(asdict(prepared))
    assert await reminders.prepare(who, body, "follow-reminder") == prepared
    assert (await reminders.list(who))[0].reminder_id == prepared.reminder_id
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.pending_reminders == []
        state.pending_reminders.append(asdict(prepared))
    assert len(await reminders.list(who)) == 1
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-1"]["status"] = "resolved"
        state.pending_reminder_cancellations.append(prepared.reminder_id)
    assert (await reminders.list(who))[0].status == "cancelled"
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.pending_reminder_cancellations == []


async def test_reminder_queue_rejects_foreign_owner_and_past_due(env):
    """协调事务内提醒仍需员工归属、未来时间和活动额度校验。"""
    from dataclasses import asdict

    from xuanpu_server.reminders.models import ReminderRecord

    control, _, service, _, _ = env
    prepared = ReminderRecord(
        "rem-q",
        "acme",
        "usr_a",
        "提醒",
        3000.0,
        "UTC",
        "once",
        ("inbox",),
        "active",
        1,
        "queue",
        1000.0,
        1000.0,
        "fp",
    )
    for record in (replace(prepared, tenant_id="other"), replace(prepared, due_at=500.0)):
        with pytest.raises(Exception):
            async with service.store.transaction("acme", "usr_a", guard) as state:
                state.pending_reminders.append(asdict(record))
                state.scan_state = {"must_rollback": True}
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.scan_state == {}
    assert await control.reminder_store.list("acme", "usr_a") == []


async def test_check_source_only_notifies_once_with_different_keys(env):
    """规则来源自身唯一，调用者更换去重键仍不能重复催办同阶段。"""
    control, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.checks["check-1"] = check(item)
        state.pending_notifications.extend(
            [notification(), notification(notification_key="another-key")]
        )
    assert len(await control.reminder_store.notifications("acme", "usr_a", 0, 100)) == 1


async def test_workspace_completion_cancels_linked_native_reminder(env):
    """待办普通完成入口同样取消已安排的关联提醒并推进关注版本。"""
    from dataclasses import asdict

    from xuanpu_server.reminders.models import ReminderRecord

    control, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    reminder = ReminderRecord(
        "rem-linked",
        "acme",
        "usr_a",
        "提醒",
        3000.0,
        "UTC",
        "once",
        ("inbox",),
        "active",
        1,
        "linked",
        1000.0,
        1000.0,
        "fp",
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.followups["follow-1"] = {
            "id": "follow-1",
            "item_id": item["id"],
            "version": 2,
            "status": "waiting",
            "reminder_id": reminder.reminder_id,
        }
        state.pending_reminders.append(asdict(reminder))
    await service.update(who, item["id"], {"status": "done"}, 1)
    assert (await control.reminder_store.list("acme", "usr_a"))[0].status == "cancelled"
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.followups["follow-1"]["version"] == 3


async def test_database_source_constraints_are_enforced(env):
    """直接数据库写入也不能越过来源互斥和跨员工复合外键。"""
    import asyncpg

    control, _, service, who, _ = env
    if not hasattr(control, "_pool"):
        pytest.skip("仅数据库约束需要直接连接")
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.checks["check-1"] = check(item)
    table = control._namespace.table("notification")
    async with control._pool.acquire() as conn:
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            await conn.execute(
                f"INSERT INTO {table} (tenant_id,user_id,notification_key,source,proactive_check_id,title,created_at) "
                "VALUES ('other','usr_a','wrong-owner','proactive_check','check-1','标题',2000)",
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"INSERT INTO {table} (tenant_id,user_id,notification_key,source,proactive_check_id,title,created_at) "
                "VALUES ('acme','usr_a','wrong-source','reminder','check-1','标题',2000)",
            )


async def test_outbox_audit_failure_rolls_back_all_domains(env, monkeypatch):
    """真实写入流程到审计失败时，提醒、通知和业务决定一起回滚。"""
    from dataclasses import asdict

    from xuanpu_server.reminders.models import ReminderRecord

    control, _, service, who, _ = env
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    reminder = ReminderRecord(
        "rem-rollback",
        "acme",
        "usr_a",
        "提醒",
        3000.0,
        "UTC",
        "once",
        ("inbox",),
        "active",
        1,
        "rollback",
        1000.0,
        1000.0,
        "fp",
    )
    if hasattr(control, "_pool"):
        ns = control._namespace
        async with control._pool.acquire() as conn:
            await conn.execute(
                f'''CREATE FUNCTION "{ns.schema}".reject_outbox_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'outbox_audit_rejected'; END; $$'''
            )
            await conn.execute(
                f'''CREATE TRIGGER reject_outbox_audit BEFORE INSERT ON {ns.table("audit")} FOR EACH ROW EXECUTE FUNCTION "{ns.schema}".reject_outbox_audit()'''
            )
    else:

        def reject(records):
            """在所有前置校验后拒绝同步提交。"""
            if records:
                raise RuntimeError("outbox_audit_rejected")

        monkeypatch.setattr(service.store, "_audit", reject)
    with pytest.raises(Exception, match="outbox_audit_rejected"):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.checks["check-1"] = check(item)
            state.scan_state = {"must_rollback": True}
            state.pending_notifications.append(notification())
            state.pending_reminders.append(asdict(reminder))
    assert len(state.pending_notifications) == 1 and len(state.pending_reminders) == 1
    assert await control.reminder_store.notifications("acme", "usr_a", 0, 100) == []
    assert await control.reminder_store.list("acme", "usr_a") == []
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.checks == {} and state.scan_state == {}


async def test_queued_request_replays_after_native_reminder_completed(env):
    """协调提交重放已经完成的原生提醒时保留原记录，不重新创建。"""
    from dataclasses import asdict

    from xuanpu_server.reminders.models import ReminderRecord

    control, _, service, _, _ = env
    record = ReminderRecord(
        "rem-completed",
        "acme",
        "usr_a",
        "提醒",
        1500.0,
        "UTC",
        "once",
        ("inbox",),
        "active",
        1,
        "completed",
        1000.0,
        1000.0,
        "fp",
    )
    await control.reminder_store.create(record, 1000.0)
    await control.reminder_store.fire(record, 2000.0)
    completed = await control.reminder_store.get("acme", "usr_a", record.reminder_id)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_reminders.append(asdict(completed))
    assert await control.reminder_store.get("acme", "usr_a", record.reminder_id) == completed


async def test_memory_disable_reenable_fences_open_snapshot(env):
    """内存停用再启用也不能让旧快照覆盖已经关闭的关注。"""
    from xuanpu_server.errors import ConflictError

    control, identity, service, who, _ = env
    if hasattr(control, "_pool"):
        pytest.skip("数据库身份行锁已由共享通知锁测试覆盖")
    item = await service.create(who, "todo", {"title": "报价"}, "todo")
    with pytest.raises(ConflictError):
        async with service.store.transaction("acme", "usr_a", guard) as state:
            state.checks["check-1"] = check(item)
            await identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=1500.0)
            await identity.set_user_status("acme", "usr_a", "active", at=1600.0)
    async with service.store.transaction("acme", "usr_a", guard) as state:
        assert state.checks == {}


@pytest.mark.parametrize(
    "linked_owner,quiet_enabled,time_text,expected_text",
    [
        ("self", True, "2026-10-20T23:00:00+08:00", "2026-10-21T08:00:00+08:00"),
        ("self", False, "2026-10-20T23:00:00+08:00", "2026-10-20T23:00:00+08:00"),
        ("self", True, "2026-10-20T12:00:00+08:00", "2026-10-20T12:00:00+08:00"),
        (None, True, "2026-10-20T23:00:00+08:00", "2026-10-20T23:00:00+08:00"),
        ("other_user", True, "2026-10-20T23:00:00+08:00", "2026-10-20T23:00:00+08:00"),
        ("other_tenant", True, "2026-10-20T23:00:00+08:00", "2026-10-20T23:00:00+08:00"),
    ],
)
async def test_linked_native_reminder_respects_proactive_quiet_hours(
    env,
    linked_owner,
    quiet_enabled,
    time_text,
    expected_text,
):
    """仅本人主动跟进安排的原生提醒延后浮层和外部投递，仍即时保存一条站内通知。"""
    from dataclasses import asdict
    from datetime import datetime

    from xuanpu_server.reminders.models import ReminderRecord

    control, _, service, who, clock = env
    due_at = datetime.fromisoformat(time_text).timestamp()
    alert_after = datetime.fromisoformat(expected_text).timestamp()
    clock[0] = due_at - 1000.0
    await service.update_settings(
        who,
        {
            "timezone": "Asia/Shanghai",
            "proactive_checks": {
                "quiet_hours": {"enabled": quiet_enabled, "start": "22:00", "end": "08:00"},
            },
        },
        1,
    )
    record = ReminderRecord(
        "rem-quiet",
        "acme",
        "usr_a",
        "报价提醒",
        due_at,
        "UTC",
        "once",
        ("inbox", "feishu"),
        "active",
        1,
        "quiet",
        clock[0],
        clock[0],
        "fp",
    )
    async with service.store.transaction("acme", "usr_a", guard) as state:
        state.pending_reminders.append(asdict(record))
    if linked_owner is not None:
        tenant, user = {
            "self": ("acme", "usr_a"),
            "other_user": ("acme", "usr_b"),
            "other_tenant": ("other", "usr_a"),
        }[linked_owner]
        async with service.store.transaction(tenant, user, guard) as state:
            state.followups["follow-quiet"] = {
                "id": "follow-quiet",
                "status": "waiting",
                "reminder_id": record.reminder_id,
            }
    notification = await control.reminder_store.fire(record, due_at)
    assert notification is not None and notification.source == "reminder"
    assert notification.created_at == due_at and notification.alert_after == alert_after
    assert notification.reminder_id == record.reminder_id
    rows = await control.reminder_store.notifications("acme", "usr_a", 0, 100)
    assert rows == [notification]
    deliveries = await control.reminder_store.deliveries(
        "acme", "usr_a", notification.notification_id
    )
    assert len(deliveries) == 1 and deliveries[0].next_attempt_at == alert_after
    if alert_after > due_at:
        assert await control.reminder_store.claim_deliveries(due_at) == []
    assert len(await control.reminder_store.claim_deliveries(alert_after)) == 1
