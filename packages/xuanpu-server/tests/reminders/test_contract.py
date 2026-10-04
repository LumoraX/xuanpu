"""提醒存储与身份停用在两个后端上的一致性契约。"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from xuanpu_server.audit import AuditLog
from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.reminders.schedule import next_due
from xuanpu_server.reminders.service import ReminderService
from xuanpu_server.tenancy import Principal, Tenant


@pytest.fixture
async def env(identity_stores):
    """构造两个租户和同名员工，以及可推进的时钟。"""
    control, identity = identity_stores
    for tenant in ("acme", "other"):
        await control.create_tenant(Tenant(tenant_id=tenant, name=tenant, created_at=1.0))
        await identity.create_user(
            UserRecord(tenant_id=tenant, user_id="usr_a", display_name="甲", created_at=1.0)
        )
    clock = [1000.0]
    service = ReminderService(
        store=control.reminder_store,
        control=control,
        identity=identity,
        audit=AuditLog(control),
        now=lambda: clock[0],
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    return service, who, clock, identity, control


def body(**changes):
    """正常的未来提醒输入。"""
    return ReminderInput(
        **dict(dict(title="提交报告", due_at=1100.0, timezone="Asia/Shanghai"), **changes)
    )


async def test_create_edit_original_idempotency_and_ownership(env):
    """编辑后原始创建幂等键仍返回当前记录，异租户不可读写。"""
    service, who, clock, _, _ = env
    record = await service.create(who, body(), "request-1")
    updated = await service.update(who, record.reminder_id, body(title="新标题"), record.version)
    clock[0] = 1200.0
    assert await service.create(who, body(), "request-1") == updated
    with pytest.raises(ConflictError):
        await service.create(who, body(title="不同"), "request-1")
    alien = Principal(role="tenant_member", tenant_id="other", user_id="usr_a", key_id=None)
    with pytest.raises(NotFoundError):
        await service.cancel(alien, record.reminder_id, record.version)


async def test_due_concurrent_exactly_once_and_read(env):
    """竞争消费同一到期提醒只产生一个通知。"""
    service, who, clock, _, _ = env
    record = await service.create(who, body(), "request-1")
    clock[0] = 1200.0
    await asyncio.gather(*[service.run_once() for _ in range(4)])
    notifications = await service.notifications(who)
    assert len(notifications) == 1
    assert notifications[0].due_at == 1100.0
    assert (await service.list(who))[0].status == "completed"
    assert (await service.read(who, notifications[0].notification_id)).read_at == 1200.0
    assert await service.notifications(who, after_id=notifications[0].notification_id) == []
    with pytest.raises(ConflictError):
        await service.cancel(who, record.reminder_id, 2)


async def test_disable_cancels_atomically_and_reenable_does_not_revive(env):
    """停用提交时活动提醒已经取消，重新启用不会复活。"""
    service, who, clock, identity, control = env
    record = await service.create(who, body(), "request-1")
    await identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=1010.0)
    stored = await control.reminder_store.get("acme", "usr_a", record.reminder_id)
    assert stored.status == "cancelled"
    await identity.set_user_status("acme", "usr_a", "active", at=1020.0)
    clock[0] = 1200.0
    await service.run_once()
    assert await service.notifications(who) == []


async def test_concurrent_limit_and_stale_update(env):
    """第100条额度只能被一个并发创建者占用。"""
    service, who, _, _, _ = env
    for i in range(99):
        await service.create(who, body(), f"initial-{i}")
    results = await asyncio.gather(
        *[service.create(who, body(), f"last-{i}") for i in range(2)], return_exceptions=True
    )
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    record = (await service.list(who))[0]
    await service.update(who, record.reminder_id, body(title="编辑"), record.version)
    with pytest.raises(ConflictError):
        await service.cancel(who, record.reminder_id, record.version)


async def test_api_key_requires_employee(env):
    """没有员工身份的 API key 不能创建提醒。"""
    service, _, _, _, _ = env
    with pytest.raises(PermissionDeniedError):
        await service.create(
            Principal(role="tenant_admin", tenant_id="acme", key_id="key"), body(), "request"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"title": " "},
        {"due_at": float("nan")},
        {"due_at": "1100"},
        {"timezone": "bad/zone"},
        {"channels": ["feishu"]},
        {"repeat": "monthly"},
        {"user_id": "usr_a"},
    ],
)
def test_strict_input(changes):
    """无效或注入身份的输入被拒绝。"""
    with pytest.raises(ValidationError):
        body(**changes)


def test_repeat_dst_gap_and_fold():
    """跳过不存在的当地时间，重叠时间一天只触发一次。"""
    zone = ZoneInfo("America/New_York")
    due = datetime(2026, 3, 7, 2, 30, tzinfo=zone).timestamp()
    assert (
        next_due(due, "America/New_York", "daily", due)
        == datetime(2026, 3, 9, 2, 30, tzinfo=zone).timestamp()
    )
    due = datetime(2026, 11, 1, 1, 30, tzinfo=zone).timestamp()
    assert (
        next_due(due, "America/New_York", "daily", due)
        == datetime(2026, 11, 2, 1, 30, tzinfo=zone).timestamp()
    )


class Sender:
    """可脚本化且保留外部通知标识的发送器。"""

    def __init__(self):
        self.calls = []
        self.failures = []
        self.enabled = True

    async def available(self, who):
        """模拟已配置的员工外部渠道。"""
        return ["feishu"] if self.enabled else []

    async def send(self, notification, channel):
        """记录稳定幂等键，并按脚本抛出安全错误。"""
        self.calls.append((notification.notification_id, channel))
        if self.failures:
            raise self.failures.pop(0)


def with_sender(env, sender):
    """用同一存储模拟服务重启并装配外部发送器。"""
    _, _, clock, identity, control = env
    return ReminderService(
        store=control.reminder_store,
        control=control,
        identity=identity,
        audit=AuditLog(control),
        sender=sender,
        now=lambda: clock[0],
    )


async def test_original_idempotency_survives_unavailable_channel(env):
    """原创建重试不因外部配置消失而失败，也不创建第二条。"""
    sender = Sender()
    service = with_sender(env, sender)
    _, who, clock, _, _ = env
    payload = body(channels=["inbox", "feishu"])
    record = await service.create(who, payload, "original")
    sender.enabled = False
    clock[0] = 1200
    assert await service.create(who, payload, "original") == record


async def test_delivery_retries_and_restart_recover(env):
    """重启从持久outbox接手，最多五次，通知幂等键始终相同。"""
    from xuanpu_server.reminders.models import DeliveryError

    sender = Sender()
    sender.failures = [DeliveryError("temporary_failure", retryable=True) for _ in range(5)]
    service = with_sender(env, sender)
    _, who, clock, _, control = env
    await service.create(who, body(channels=["inbox", "feishu"]), "outbox")
    clock[0] = 1200
    for delay in (0, 1, 5, 30, 120):
        clock[0] += delay
        service = with_sender(env, sender)
        await service.run_once()
    notifications = await service.notifications(who)
    assert len(notifications) == 1
    deliveries = await control.reminder_store.deliveries(
        "acme", "usr_a", notifications[0].notification_id
    )
    assert deliveries[0].status == "failed"
    assert deliveries[0].attempts == 5
    assert len(sender.calls) == 5
    assert len(set(sender.calls)) == 1
    clock[0] += 500
    await service.run_once()
    assert len(sender.calls) == 5


async def test_claim_token_rejects_late_worker_and_disable_skips(env):
    """过期工作者不能覆盖新租约，停用同时跳过外部发送。"""
    sender = Sender()
    service = with_sender(env, sender)
    _, who, clock, identity, control = env
    record = await service.create(who, body(channels=["inbox", "feishu"]), "lease")
    store = control.reminder_store
    await store.fire(record, 1200)
    first = (await store.claim_deliveries(1200))[0][0]
    assert await store.claim_deliveries(1201) == []
    second = (await store.claim_deliveries(1230))[0][0]
    assert first.claim_token != second.claim_token
    await store.finish_delivery(first, "sent", 1231)
    current = (await store.deliveries("acme", "usr_a", first.notification_id))[0]
    assert current.status == "sending" and current.claim_token == second.claim_token
    await identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=1232)
    current = (await store.deliveries("acme", "usr_a", first.notification_id))[0]
    assert current.status == "skipped"
    await store.finish_delivery(second, "sent", 1233)
    assert (await store.deliveries("acme", "usr_a", first.notification_id))[0].status == "skipped"


async def test_due_cancel_and_disable_races_leave_no_active_reminder(env):
    """到期与取消或停用竞争不会留下活动提醒或重复通知。"""
    service, who, clock, identity, control = env
    record = await service.create(who, body(), "race")
    clock[0] = 1200
    await asyncio.gather(
        service.run_once(), service.cancel(who, record.reminder_id, 1), return_exceptions=True
    )
    assert len(await service.notifications(who)) <= 1
    stored = await control.reminder_store.get("acme", "usr_a", record.reminder_id)
    assert stored.status in ("cancelled", "completed")
    next_record = await service.create(who, body(due_at=1300.0, repeat="daily"), "disable-race")
    clock[0] = 1400
    await asyncio.gather(
        service.run_once(),
        identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=1400),
    )
    stored = await control.reminder_store.get("acme", "usr_a", next_record.reminder_id)
    assert stored.status == "cancelled"


async def test_repeat_late_coalesces_and_latest_window(env):
    """长时间停机合并一次通知并跳到未来，最新窗口仍按游标升序。"""
    service, who, clock, _, _ = env
    await service.create(who, body(repeat="daily"), "repeat")
    for i in range(3):
        clock[0] = 1100 + (i + 5) * 86400
        await service.run_once()
    notifications = await service.notifications(who)
    assert len(notifications) == 3
    assert (await service.list(who))[0].due_at > clock[0]
    assert await service.notifications(who, limit=2, latest=True) == notifications[-2:]


async def test_delivery_finish_rechecks_tenant(env):
    """外部成功事实可在租户暂停后回写，避免恢复时重复外发。"""
    from dataclasses import replace

    sender = Sender()
    service = with_sender(env, sender)
    _, who, _, _, control = env
    record = await service.create(who, body(channels=["inbox", "feishu"]), "tenant-stop")
    await service.store.fire(record, 1200)
    delivery = (await service.store.claim_deliveries(1200))[0][0]
    tenant = await control.get_tenant("acme")
    await control.update_tenant(replace(tenant, status="disabled"))
    await service.store.finish_delivery(delivery, "sent", 1201)
    assert (await service.store.deliveries("acme", "usr_a", delivery.notification_id))[
        0
    ].status == "sent"


async def test_worker_does_not_send_nearly_expired_lease(env):
    """没有足够完成10秒网络调用的租约，不启动外部副作用。"""
    sender = Sender()
    service = with_sender(env, sender)
    _, who, clock, _, _ = env
    record = await service.create(who, body(channels=["inbox", "feishu"]), "old-worker")
    await service.store.fire(record, 1200)
    delivery, notification = (await service.store.claim_deliveries(1200))[0]
    clock[0] = 1225
    await service._worker._send(delivery, notification)
    assert sender.calls == []


def test_due_horizon_is_five_calendar_years():
    """五年边界按日历而不是把每一年都当作366天。"""
    from xuanpu_server.errors import InvalidRequestError
    from xuanpu_server.reminders.schedule import validate_due

    now = datetime(2026, 10, 5, tzinfo=ZoneInfo("UTC")).timestamp()
    edge = datetime(2031, 10, 5, tzinfo=ZoneInfo("UTC")).timestamp()
    validate_due(edge, now)
    with pytest.raises(InvalidRequestError):
        validate_due(edge + 1, now)


async def test_tenant_pause_resume_catches_up_without_cancelling(env):
    """租户暂停保留活动提醒，恢复后一次补发；员工停用仍永久取消。"""
    from dataclasses import replace

    service, who, clock, _, control = env
    record = await service.create(who, body(repeat="daily"), "tenant-pause")
    tenant = await control.get_tenant("acme")
    await control.update_tenant(replace(tenant, status="disabled"))
    clock[0] = 1100 + 5 * 86400
    await service.run_once()
    assert (await service.store.get("acme", "usr_a", record.reminder_id)).status == "active"
    await control.update_tenant(tenant)
    await service.run_once()
    found = await service.notifications(who)
    assert len(found) == 1 and found[0].due_at == clock[0]


async def test_status_filter_before_page_limit(env):
    """先筛选状态再分页，长期活动提醒不会被较新的已完成记录挤掉。"""
    service, who, clock, _, _ = env
    oldest = await service.create(who, body(due_at=2000.0), "oldest")
    for i in range(100):
        clock[0] += 1
        record = await service.create(who, body(due_at=clock[0] + 0.5), f"recent-{i}")
        await service.store.fire(record, clock[0] + 0.5)
    assert await service.list(who, status="active") == [oldest]


async def test_outbox_restart_past_window_records_unknown(env):
    """有过发送尝试的任务重启超一小时不再自动外发。"""
    from xuanpu_server.reminders.models import DeliveryError

    sender = Sender()
    sender.failures = [DeliveryError("delivery_timeout", retryable=True)]
    service = with_sender(env, sender)
    _, who, clock, _, _ = env
    await service.create(who, body(channels=["inbox", "feishu"]), "window")
    clock[0] = 1200
    await service.run_once()
    clock[0] += 7 * 86400
    service = with_sender(env, sender)
    await service.run_once()
    notification = (await service.notifications(who))[0]
    delivery = (await service.deliveries(who, notification.notification_id))[0]
    assert len(sender.calls) == 1
    assert delivery.status == "failed" and delivery.error_code == "delivery_unknown"


async def test_crud_and_fire_audit_exactly_once_no_content(env):
    """审计与域写入同事务，去重不会重复审计且不包含提醒正文。"""
    from xuanpu_server.controlplane.models import AuditQuery

    service, who, clock, _, control = env
    record = await service.create(who, body(), "audit")
    await service.create(who, body(), "audit")
    edited = await service.update(who, record.reminder_id, body(title="敏感正文"), record.version)
    clock[0] = 1200
    await asyncio.gather(service.store.fire(edited, 1200), service.store.fire(edited, 1200))
    events = await control.query_audit("acme", AuditQuery(user_id="usr_a"))
    assert [e.kind for e in events] == ["reminder.created", "reminder.updated", "reminder.fired"]
    assert all("敏感正文" not in str(e.data) and e.user_id == "usr_a" for e in events)
    assert events[-1].actor == "system:reminders"


@pytest.mark.parametrize("operation", ["create", "update", "cancel", "fire"])
async def test_audit_failure_rolls_back_domain_mutation(env, monkeypatch, operation):
    """审计写入失败时创建、编辑、取消和到期都不能留下半写结果。"""
    from xuanpu_server.controlplane.memory import InMemoryControlPlane

    service, who, _, _, control = env
    record = None if operation == "create" else await service.create(who, body(), "audit-failure")
    if isinstance(control, InMemoryControlPlane):

        def reject(record):
            """注入同步审计故障。"""
            raise RuntimeError("audit_rejected")

        monkeypatch.setattr(control, "_append_reminder_audit", reject, raising=False)
    else:
        namespace = control._namespace
        async with control._pool.acquire() as conn:
            await conn.execute(
                f'''CREATE FUNCTION "{namespace.schema}".reject_reminder_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit_rejected'; END; $$'''
            )
            await conn.execute(
                f'''CREATE TRIGGER reject_reminder_audit BEFORE INSERT ON {namespace.table("audit")} FOR EACH ROW EXECUTE FUNCTION "{namespace.schema}".reject_reminder_audit()'''
            )
    with pytest.raises(Exception, match="audit_rejected"):
        if operation == "create":
            await service.create(who, body(), "audit-failure")
        elif operation == "update":
            await service.update(who, record.reminder_id, body(title="修改"), record.version)
        elif operation == "cancel":
            await service.cancel(who, record.reminder_id, record.version)
        else:
            await service.store.fire(record, 1200)
    assert await service.notifications(who) == []
    assert await service.list(who) == ([] if record is None else [record])


async def test_unattempted_outbox_can_start_after_long_pause(env):
    """从未认领过的通知延迟数天，恢复后仍可首次发送。"""
    from dataclasses import replace

    sender = Sender()
    service = with_sender(env, sender)
    _, who, clock, _, control = env
    record = await service.create(who, body(channels=["inbox", "feishu"]), "unattempted")
    await service.store.fire(record, 1200)
    tenant = await control.get_tenant("acme")
    await control.update_tenant(replace(tenant, status="disabled"))
    clock[0] = 1200 + 7 * 86400
    await service.run_once()
    assert sender.calls == []
    await control.update_tenant(tenant)
    await service.run_once()
    assert len(sender.calls) == 1


async def test_paused_tenant_does_not_starve_global_scans(env):
    """暂停租户的一百条旧提醒和一百条旧投递不能挡住其他租户。"""
    from dataclasses import replace

    sender = Sender()
    service = with_sender(env, sender)
    _, who, clock, _, control = env
    for i in range(100):
        record = await service.create(who, body(channels=["inbox", "feishu"]), f"out-{i}")
        await service.store.fire(record, 1200)
        await service.create(who, body(), f"due-{i}")
    other = Principal(role="tenant_member", tenant_id="other", user_id="usr_a", key_id=None)
    await service.create(other, body(due_at=1150.0, channels=["inbox", "feishu"]), "other")
    tenant = await control.get_tenant("acme")
    await control.update_tenant(replace(tenant, status="disabled"))
    clock[0] = 1200
    await service.run_once()
    assert len(await service.notifications(other)) == 1
    assert len(sender.calls) == 1


async def test_failed_delivery_write_cancels_other_network_tasks(env, monkeypatch):
    """一条回写异常时，当前批次其他网络任务必须取消并等待结束。"""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    class BlockingSender(Sender):
        """首条等另一条进入网络，另一条永久等待直到取消。"""

        async def send(self, notification, channel):
            """制造回写失败与另一条仍在发送的确定性并发。"""
            if notification.title == "first":
                await started.wait()
                return
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    service = with_sender(env, BlockingSender())
    _, who, clock, _, _ = env
    for i, title in enumerate(("first", "second")):
        await service.create(who, body(title=title, channels=["inbox", "feishu"]), f"cancel-{i}")

    async def fail_finish(*args, **kwargs):
        """模拟投递回写数据库故障。"""
        raise RuntimeError("write_failed")

    monkeypatch.setattr(service.store, "finish_delivery", fail_finish)
    clock[0] = 1200
    with pytest.raises(ExceptionGroup):
        await service.run_once()
    assert cancelled.is_set()
    await service.close()


@pytest.mark.parametrize("prior_attempt", [False, True])
async def test_pause_before_network_resets_window_only_without_prior_attempt(env, prior_attempt):
    """首次认领未发出便暂停可清窗口；存在历史尝试则必须保留窗口。"""
    from dataclasses import replace

    from xuanpu_server.reminders.models import DeliveryError

    sender = Sender()
    service = with_sender(env, sender)
    _, who, clock, _, control = env
    record = await service.create(who, body(channels=["inbox", "feishu"]), "pause-window")
    await service.store.fire(record, 1200)
    clock[0] = 1200
    if prior_attempt:
        sender.failures = [DeliveryError("delivery_timeout", retryable=True)]
        await service.run_once()
        clock[0] += 1
    delivery, notification = (await service.store.claim_deliveries(clock[0]))[0]
    tenant = await control.get_tenant("acme")
    await control.update_tenant(replace(tenant, status="disabled"))
    await service._worker._send(delivery, notification)
    pending = (await service.store.deliveries("acme", "usr_a", notification.notification_id))[0]
    assert pending.status == "pending"
    assert pending.attempts == int(prior_attempt)
    assert pending.first_attempt_at == (1200 if prior_attempt else None)
    assert len(sender.calls) == int(prior_attempt)
    clock[0] = 5000
    await control.update_tenant(tenant)
    await service.run_once()
    finished = (await service.deliveries(who, notification.notification_id))[0]
    if prior_attempt:
        assert finished.status == "failed" and finished.error_code == "delivery_unknown"
        assert len(sender.calls) == 1
    else:
        assert finished.status == "sent" and finished.first_attempt_at == 5000
        assert len(sender.calls) == 1


async def test_slow_sender_does_not_block_inbox_scan_and_close_cancels(env):
    """外部发送持续等待时站内扫描仍按秒运行，关闭回收全部网络任务。"""
    entered, cancelled = asyncio.Event(), asyncio.Event()

    class SlowSender(Sender):
        """把外部网络保持在等待状态。"""

        async def send(self, notification, channel):
            """收到取消时记录，验证生命周期归属。"""
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    service = with_sender(env, SlowSender())
    _, who, clock, _, _ = env
    external = await service.create(who, body(channels=["inbox", "feishu"]), "slow-external")
    await service.store.fire(external, 1200)
    clock[0] = 1200
    pending = await service.create(who, body(due_at=1201.0), "next-inbox")
    service.start()
    try:
        await asyncio.wait_for(entered.wait(), 2)
        clock[0] = 1202

        async def wait_for_inbox():
            """等待持久通知出现而非仅等待调度函数被调用。"""
            while True:
                if any(
                    n.reminder_id == pending.reminder_id for n in await service.notifications(who)
                ):
                    return
                await asyncio.sleep(0.02)

        await asyncio.wait_for(wait_for_inbox(), 2.5)
    finally:
        await service.close()
    assert cancelled.is_set()
