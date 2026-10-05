"""个人工作台在内存和 PostgreSQL 上的行为契约。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.objectstore import LocalObjectStore
from xuanpu_server.tenancy import Principal, Tenant


@pytest.fixture
async def env(identity_stores, tmp_path):
    """真实身份、双后端和本机对象正本。"""
    from xuanpu_server.workspace.service import WorkspaceService

    control, identity = identity_stores
    await control.create_tenant(Tenant(tenant_id="acme", name="公司", created_at=1.0))
    for user in ("usr_a", "usr_b"):
        await identity.create_user(
            UserRecord(tenant_id="acme", user_id=user, display_name=user, created_at=1.0)
        )
    service = WorkspaceService(
        store=control.workspace_store,
        control=control,
        identity=identity,
        objects=LocalObjectStore(tmp_path / "objects"),
        now=lambda: 1000.0,
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_a", key_id=None)
    return service, who, control, identity


async def test_typed_crud_idempotency_and_isolation(env):
    """严格类型、请求幂等和本人边界一致。"""
    service, who, _, identity = env
    item = await service.create(who, "todo", {"title": "工作"}, "one")
    assert item["data"]["status"] == "open"
    edited = await service.update(who, item["id"], {"status": "done"}, 1)
    assert edited["version"] == 2
    assert (await service.create(who, "todo", {"title": "工作"}, "one"))["id"] == item["id"]
    with pytest.raises(ConflictError):
        await service.create(who, "todo", {"title": "另一个"}, "one")
    with pytest.raises(ConflictError):
        await service.update(who, item["id"], {"title": "并发"}, 1)
    for data in (
        {"title": "", "status": "open"},
        {"title": "x", "user_id": "usr_b"},
        {"title": "x", "due_at": True},
    ):
        with pytest.raises(InvalidRequestError):
            await service.create(who, "todo", data, "invalid")
    other = replace(who, user_id="usr_b", role="tenant_admin")
    assert await service.list(other) == []
    with pytest.raises(NotFoundError):
        await service.get(other, item["id"])
    with pytest.raises(PermissionDeniedError):
        await service.list(replace(who, user_id=None, key_id="machine"))
    await identity.set_user_status("acme", "usr_a", "disabled", reason="admin", at=1001)
    with pytest.raises(PermissionDeniedError):
        await service.list(who)


async def test_folders_cycles_collisions_and_recursive_recycle(env):
    """目录回收按批次还原，原本已删除子项不会意外复活。"""
    service, who, _, _ = env
    folder = await service.create(who, "folder", {"name": "资料"}, "folder")
    child = await service.create(
        who, "folder", {"name": "子目录", "parent_id": folder["id"]}, "child"
    )
    file = await service.upload(who, "a.txt", b"data", "text/plain", "file", parent_id=child["id"])
    old = await service.upload(who, "old.txt", b"old", "text/plain", "old", parent_id=child["id"])
    await service.trash(who, old["id"], 1)
    with pytest.raises(ConflictError):
        await service.create(who, "folder", {"name": "资料"}, "duplicate")
    with pytest.raises(ConflictError):
        await service.update(who, folder["id"], {"parent_id": child["id"]}, 1)
    trashed = await service.trash(who, folder["id"], 1)
    assert len(await service.list(who, trashed=True)) == 4
    with pytest.raises(ConflictError):
        await service.restore(who, file["id"], 2)
    await service.restore(who, folder["id"], trashed["version"])
    assert len(await service.list(who)) == 3
    assert (await service.get(who, old["id"]))["trashed_at"] is not None
    assert (await service.download(who, file["id"]))[0] == b"data"


async def test_versions_quota_and_concurrent_cas(env):
    """替换正本保留旧版、总空间包括历史与回收站，并发只允许一次。"""
    service, who, _, _ = env
    service.quota_bytes = 10
    file = await service.upload(who, "a.txt", b"first", "text/plain", "file")
    results = await asyncio.gather(
        *[
            service.replace_content(who, file["id"], b"next", "text/plain", 1, f"edit-{i}")
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(r, ConflictError) for r in results) == 1
    assert len(await service.versions(who, file["id"])) == 2
    assert (await service.download(who, file["id"], 1))[0] == b"first"
    assert (await service.download(who, file["id"]))[0] == b"next"
    with pytest.raises(ConflictError):
        await service.replace_content(who, file["id"], b"xx", "text/plain", 2, "quota")
    with pytest.raises(InvalidRequestError):
        await service.update(who, file["id"], {"size": 0}, 2)
    await service.trash(who, file["id"], 2)
    assert (await service.summary(who))["used_bytes"] == 9
    assert (await service.summary(who))["counts"]["file"] == 0


async def test_links_settings_messages_and_minimal_audit(env):
    """关联、设置和主对话均持久保存且审计不泄露正文。"""
    from xuanpu_server.controlplane.models import AuditQuery

    service, who, control, _ = env
    matter = await service.create(who, "matter", {"title": "项目"}, "matter")
    todo = await service.create(who, "todo", {"title": "任务", "matter_id": matter["id"]}, "todo")
    with pytest.raises(ConflictError):
        await service.trash(who, matter["id"], 1)
    file = await service.upload(who, "file.txt", b"secret file", "text/plain", "file")
    message = await service.append_message(
        who, "user", "敏感正文", "message", [file["id"]], matter["id"]
    )
    assert (
        await service.append_message(who, "user", "敏感正文", "message", [file["id"]], matter["id"])
        == message
    )
    assert await service.messages(who) == [message]
    assert await service.messages(who, message["id"]) == []
    assert await service.messages(replace(who, user_id="usr_b")) == []
    with pytest.raises(NotFoundError):
        await service.append_message(
            replace(who, user_id="usr_b"), "user", "x", "other", [file["id"]]
        )
    settings = await service.settings(who)
    assert settings["version"] == 1
    saved = await service.update_settings(
        who, {"assistant_name": "助理", "user_title": "老板", "style": "简洁", "timezone": "UTC"}, 1
    )
    assert saved["version"] == 2 and (await service.settings(who)) == saved
    with pytest.raises(ConflictError):
        await service.update_settings(who, {"assistant_name": "旧版本"}, 1)
    with pytest.raises(InvalidRequestError):
        await service.create(who, "file", {"name": "x", "source_url": "http://127.0.0.1"}, "url")
    for name in ("../x", "a/b", "a\\b", "a\r\n.txt"):
        with pytest.raises(InvalidRequestError):
            await service.upload(who, name, b"x", "text/plain", "badname")
    audits = await control.query_audit("acme", AuditQuery(user_id="usr_a"))
    assert audits and all("敏感正文" not in str(a.data) for a in audits)
    await service.update(who, todo["id"], {"matter_id": None}, 1)
    await service.trash(who, matter["id"], 1)


async def test_message_lookup_latest_and_server_metadata(env):
    """重试按请求索引读取助手结果，最近窗口按升序返回。"""
    service, who, _, _ = env
    assert await service.find_message(who, "route:assistant") is None
    for i in range(3):
        await service.append_message(who, "user", f"消息{i}", f"route{i}:user")
    result = await service.append_message(
        who,
        "assistant",
        "结果",
        "route:assistant",
        metadata={"message": "结果", "open_workspace": "todo"},
    )
    assert await service.find_message(who, "route:assistant") == result
    assert await service.find_message(replace(who, user_id="usr_b"), "route:assistant") is None
    assert [v["id"] for v in await service.messages(who, limit=2, latest=True)] == [3, 4]
    with pytest.raises(InvalidRequestError):
        await service.append_message(
            who, "assistant", "大响应", "large", metadata={"x": "a" * 70000}
        )


async def test_restore_collision_rolls_back_whole_batch(env):
    """恢复遇到同名目录时不留下部分恢复的子节点。"""
    service, who, _, _ = env
    folder = await service.create(who, "folder", {"name": "资料"}, "first")
    child = await service.upload(
        who, "doc.txt", b"content", "text/plain", "child", parent_id=folder["id"]
    )
    await service.trash(who, folder["id"], 1)
    await service.create(who, "folder", {"name": "资料"}, "second")
    with pytest.raises(ConflictError):
        await service.restore(who, folder["id"], 2)
    assert (await service.get(who, child["id"]))["trashed_at"] is not None
    assert (await service.get(who, child["id"]))["version"] == 2


async def test_content_request_replay_and_bounded_empty_versions(env):
    """重试内容替换不重复计算空间，空版本也有总数量上限。"""
    service, who, _, _ = env
    service.max_versions = 2
    file = await service.upload(who, "data.bin", b"a", "application/octet-stream", "first")
    assert await service.upload(who, "data.bin", b"a", "application/octet-stream", "first") == file
    changed = await service.replace_content(
        who, file["id"], b"", "application/octet-stream", 1, "second"
    )
    assert (
        await service.replace_content(who, file["id"], b"", "application/octet-stream", 1, "second")
        == changed
    )
    with pytest.raises(ConflictError):
        await service.replace_content(
            who, file["id"], b"different", "application/octet-stream", 1, "second"
        )
    with pytest.raises(ConflictError):
        await service.replace_content(who, file["id"], b"", "application/octet-stream", 2, "third")
    service.max_file_bytes = 2
    with pytest.raises(InvalidRequestError):
        await service.upload(who, "large.bin", b"abc", "application/octet-stream", "large")


@pytest.mark.parametrize(
    "kind,data",
    [
        ("event", {"title": "会议", "start_at": 1000, "end_at": 999}),
        ("event", {"title": "会议", "start_at": 1000, "end_at": 2000, "timezone": "Bogus/Zone"}),
        ("skill", {"title": "写作", "instructions": ""}),
        ("skill", {"title": "写作", "instructions": "检查材料", "enabled": "yes"}),
        ("matter", {"title": "项目", "status": "done"}),
    ],
)
async def test_independent_domain_validation(env, kind, data):
    """不同领域不能绕过各自的类型、枚举和时间约束。"""
    service, who, _, _ = env
    with pytest.raises(InvalidRequestError):
        await service.create(who, kind, data, "invalid")


async def test_cross_tenant_and_inactive_tenant(env):
    """同名员工不能跨租户查询，暂停租户立即拒绝读写。"""
    service, who, control, identity = env
    await control.create_tenant(Tenant(tenant_id="other", name="其他", created_at=1))
    await identity.create_user(
        UserRecord(tenant_id="other", user_id="usr_a", display_name="同名", created_at=1)
    )
    file = await service.upload(who, "private.txt", b"private", "text/plain", "private")
    alien = replace(who, tenant_id="other")
    with pytest.raises(NotFoundError):
        await service.download(alien, file["id"])
    tenant = await control.get_tenant("acme")
    await control.update_tenant(replace(tenant, status="suspended"))
    with pytest.raises(PermissionDeniedError):
        await service.settings(who)


async def test_audit_failure_rolls_back_metadata_and_idempotency(env, monkeypatch):
    """事务审计故障不留下可见记录、空间统计或幂等响应。"""
    from xuanpu_server.controlplane.memory import InMemoryControlPlane

    service, who, control, _ = env
    if isinstance(control, InMemoryControlPlane):

        def reject(records):
            """仅真实写审计时注入失败。"""
            if records:
                raise RuntimeError("audit_rejected")

        monkeypatch.setattr(service.store, "_audit", reject)
    else:
        namespace = control._namespace
        async with control._pool.acquire() as conn:
            await conn.execute(
                f'''CREATE FUNCTION "{namespace.schema}".reject_workspace_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit_rejected'; END; $$'''
            )
            await conn.execute(
                f'''CREATE TRIGGER reject_workspace_audit BEFORE INSERT ON {namespace.table("audit")} FOR EACH ROW EXECUTE FUNCTION "{namespace.schema}".reject_workspace_audit()'''
            )
    with pytest.raises(Exception, match="audit_rejected"):
        await service.upload(who, "private.txt", b"private", "text/plain", "failure")
    assert await service.list(who) == []
    assert (await service.summary(who))["used_bytes"] == 0
    assert await service.find_message(who, "failure") is None


async def test_concurrent_create_idempotency_and_quota(env):
    """重复上传只占一份，独立上传不能竞争穿透个人空间。"""
    service, who, _, _ = env
    service.quota_bytes = 10
    results = await asyncio.gather(
        *[service.upload(who, "same.txt", b"first", "text/plain", "same") for _ in range(4)]
    )
    assert len({v["id"] for v in results}) == 1
    uploads = await asyncio.gather(
        *[
            service.upload(who, f"next-{i}.txt", b"next", "text/plain", f"next-{i}")
            for i in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(v, ConflictError) for v in uploads) == 1
    assert (await service.summary(who))["used_bytes"] == 9


async def test_complete_domain_records_and_search(env):
    """事项、待办、日程和技能均可编辑查询并独立回收。"""
    service, who, _, _ = env
    event = await service.create(
        who, "event", {"title": "周会", "start_at": 1000, "end_at": 2000}, "event"
    )
    skill = await service.create(
        who, "skill", {"title": "报告整理", "instructions": "先检查材料"}, "skill"
    )
    link = await service.create(
        who,
        "file",
        {"name": "参考文档", "source_url": "https://docs.example.com/reference"},
        "link",
    )
    assert link["data"]["current_revision"] == 0
    assert (await service.update(who, event["id"], {"end_at": 3000}, 1))["data"]["end_at"] == 3000
    assert (await service.update(who, skill["id"], {"enabled": False}, 1))["data"][
        "enabled"
    ] is False
    assert [v["id"] for v in await service.list(who, search="报告")] == [skill["id"]]
    assert len(await service.list(who, kind="event")) == 1
    with pytest.raises(NotFoundError):
        await service.download(who, link["id"])


async def test_pending_proposals_filter_before_limit_and_owner_isolation(env):
    """待确认记录先过滤终态再分页，不被后续闲聊挤掉。"""
    service, who, _, _ = env
    proposal = {"route": {"workspace_draft": {"kind": "todo", "data": {"title": "任务"}}}}
    pending = await service.append_message(
        who, "assistant", "请确认", "old:assistant", metadata=proposal
    )
    await service.append_message(who, "assistant", "已处理", "done:assistant", metadata=proposal)
    await service.append_message(
        who, "assistant", "确认完成", "done:confirmed", metadata={"result": {"id": "created"}}
    )
    await service.append_message(who, "assistant", "将取消", "cancel:assistant", metadata=proposal)
    await service.append_message(who, "assistant", "已取消", "cancel:cancelled")
    for i in range(101):
        await service.append_message(
            who,
            "assistant",
            "闲聊",
            f"chat-{i}:assistant",
            metadata={"route": {"workspace_draft": None, "reminder_draft": None}},
        )
    assert await service.pending_proposals(who) == [pending]
    assert await service.pending_proposals(replace(who, user_id="usr_b")) == []
    reminder = await service.append_message(
        who,
        "assistant",
        "提醒建议",
        "rem:assistant",
        metadata={"route": {"reminder_draft": {"title": "提醒"}}},
    )
    assert await service.pending_proposals(who) == [reminder, pending]


async def test_proposal_reservation_freezes_action_and_data_across_races(env):
    """跨实例确认和取消只有一个能预留，失败重试只能沿用原内容。"""
    service, who, _, _ = env
    data = {"kind": "todo", "data": {"title": "任务"}}
    await service.reserve_proposal(who, "frozen", "confirm", data)
    await service.reserve_proposal(who, "frozen", "confirm", data)
    with pytest.raises(ConflictError):
        await service.reserve_proposal(who, "frozen", "cancel", {})
    with pytest.raises(ConflictError):
        await service.reserve_proposal(
            who, "frozen", "confirm", {"kind": "todo", "data": {"title": "变化"}}
        )
    results = await asyncio.gather(
        service.reserve_proposal(who, "race", "confirm", data),
        service.reserve_proposal(who, "race", "cancel", {}),
        return_exceptions=True,
    )
    assert sum(isinstance(v, ConflictError) for v in results) == 1
    await service.reserve_proposal(replace(who, user_id="usr_b"), "frozen", "cancel", {})
    with pytest.raises(InvalidRequestError):
        await service.reserve_proposal(who, "unknown", "execute", {})


async def test_non_message_request_cannot_hide_pending_proposal(env):
    """普通记录偶然使用终态形状请求键不等于建议已处理。"""
    service, who, _, _ = env
    pending = await service.append_message(
        who,
        "assistant",
        "请确认",
        "independent:assistant",
        metadata={"route": {"workspace_draft": {"kind": "todo", "data": {"title": "待确认"}}}},
    )
    await service.create(who, "todo", {"title": "独立"}, "independent:confirmed")
    assert await service.pending_proposals(who) == [pending]


async def test_failed_native_confirmation_can_change_content_or_cancel(env):
    """原生创建失败回滚确认决定，改名或取消都不会被旧输入冻结。"""
    service, who, _, _ = env
    await service.create(who, "folder", {"name": "已有"}, "existing")
    for proposal_id in ("rename", "cancel-failed"):
        with pytest.raises(ConflictError):
            await service.create(
                who, "folder", {"name": "已有"}, proposal_id + ":create", proposal_id=proposal_id
            )
    created = await service.create(
        who, "folder", {"name": "新名"}, "rename:create", proposal_id="rename"
    )
    assert created["data"]["name"] == "新名"
    assert (
        await service.create(who, "folder", {"name": "新名"}, "rename:create", proposal_id="rename")
        == created
    )
    with pytest.raises(ConflictError):
        await service.create(
            who, "folder", {"name": "再次修改"}, "rename:create", proposal_id="rename"
        )
    await service.reserve_proposal(who, "cancel-failed", "cancel", {})
    with pytest.raises(ConflictError):
        await service.create(
            who, "folder", {"name": "已取消后"}, "cancel-failed:create", proposal_id="cancel-failed"
        )


async def test_native_confirmation_and_cancel_compete_atomically(env):
    """创建和取消共用员工锁，不能同时产生已创建和已取消两个结果。"""
    service, who, _, _ = env
    results = await asyncio.gather(
        service.create(
            who, "todo", {"title": "竞争"}, "competition:create", proposal_id="competition"
        ),
        service.reserve_proposal(who, "competition", "cancel", {}),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ConflictError) for r in results) == 1
    items = await service.list(who)
    if isinstance(results[0], dict):
        assert len(items) == 1
        with pytest.raises(ConflictError):
            await service.reserve_proposal(who, "competition", "cancel", {})
    else:
        assert items == []
        with pytest.raises(ConflictError):
            await service.create(
                who, "todo", {"title": "竞争"}, "competition:create", proposal_id="competition"
            )


def reminder_service(env):
    """共享同一身份及事务后端的提醒用例。"""
    from xuanpu_server.audit import AuditLog
    from xuanpu_server.reminders.service import ReminderService

    _, _, control, identity = env
    return ReminderService(
        store=control.reminder_store,
        control=control,
        identity=identity,
        audit=AuditLog(control),
        now=lambda: 1000.0,
    )


async def test_failed_reminder_proposal_can_change_time_or_cancel(env):
    """过期提醒创建失败不冻结确认，可修改时间或取消原建议。"""
    from xuanpu_server.reminders.models import ReminderInput

    workspace, who, _, _ = env
    reminders = reminder_service(env)
    past = ReminderInput(title="过期提醒", due_at=500.0, timezone="UTC")
    future = ReminderInput(title="未来提醒", due_at=1500.0, timezone="UTC")
    for key in ("rem-retry", "rem-cancel"):
        with pytest.raises(InvalidRequestError):
            await reminders.create(who, past, key + ":create", proposal_id=key)
    saved = await reminders.create(who, future, "rem-retry:create", proposal_id="rem-retry")
    assert await reminders.create(who, future, "rem-retry:create", proposal_id="rem-retry") == saved
    with pytest.raises(ConflictError):
        await workspace.reserve_proposal(who, "rem-retry", "cancel", {})
    await workspace.reserve_proposal(who, "rem-cancel", "cancel", {})
    with pytest.raises(ConflictError):
        await reminders.create(who, future, "rem-cancel:create", proposal_id="rem-cancel")
    assert len(await reminders.list(who)) == 1


async def test_reminder_confirmation_and_cancel_compete_atomically(env):
    """提醒确认和工作台取消在两个后端都互斥，创建最多一次。"""
    from xuanpu_server.reminders.models import ReminderInput

    workspace, who, _, _ = env
    reminders = reminder_service(env)
    body = ReminderInput(title="竞争提醒", due_at=1500.0, timezone="UTC")
    outcomes = await asyncio.gather(
        reminders.create(who, body, "rem-race:create", proposal_id="rem-race"),
        workspace.reserve_proposal(who, "rem-race", "cancel", {}),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ConflictError) for r in outcomes) == 1
    found = await reminders.list(who)
    if isinstance(outcomes[0], ConflictError):
        assert found == []
    else:
        assert len(found) == 1


async def test_matter_messages_filter_before_recent_window(env):
    """事项历史先按本人事项过滤，再取最新窗口。"""
    service, who, _, _ = env
    matter = await service.create(who, "matter", {"title": "项目"}, "matter-filter")
    first = await service.append_message(
        who, "user", "事项第一条", "matter-one", matter_id=matter["id"]
    )
    await service.append_message(who, "user", "无关聊天", "unrelated")
    second = await service.append_message(
        who, "assistant", "事项第二条", "matter-two", matter_id=matter["id"]
    )
    await service.append_message(who, "user", "更多无关聊天", "unrelated-two")
    assert await service.messages(who, matter_id=matter["id"]) == [first, second]
    assert await service.messages(who, limit=1, latest=True, matter_id=matter["id"]) == [second]
    with pytest.raises(NotFoundError):
        await service.messages(replace(who, user_id="usr_b"), matter_id=matter["id"])


@pytest.mark.parametrize("domain", ["native", "reminder"])
async def test_proposal_audit_failure_rolls_back_decision_and_creation(env, monkeypatch, domain):
    """决定和领域审计失败时二者一起回滚，之后仍允许取消。"""
    from xuanpu_server.controlplane.memory import InMemoryControlPlane
    from xuanpu_server.reminders.models import ReminderInput

    workspace, who, control, _ = env
    reminders = reminder_service(env)
    original_audit = None
    if isinstance(control, InMemoryControlPlane):
        original_audit = workspace.store._audit

        def reject(records):
            """拒绝真实领域审计，不影响只读事务。"""
            if records:
                raise RuntimeError("proposal_audit_failed")

        monkeypatch.setattr(workspace.store, "_audit", reject)
    else:
        ns = control._namespace
        async with control._pool.acquire() as conn:
            await conn.execute(
                f'''CREATE FUNCTION "{ns.schema}".reject_proposal_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'proposal_audit_failed'; END; $$'''
            )
            await conn.execute(
                f'''CREATE TRIGGER reject_proposal_audit BEFORE INSERT ON {ns.table("audit")} FOR EACH ROW EXECUTE FUNCTION "{ns.schema}".reject_proposal_audit()'''
            )
    with pytest.raises(Exception, match="proposal_audit_failed"):
        if domain == "native":
            await workspace.create(
                who,
                "todo",
                {"title": "原子失败"},
                "audit-proposal:create",
                proposal_id="audit-proposal",
            )
        else:
            await reminders.create(
                who,
                ReminderInput(title="原子失败", due_at=1500.0, timezone="UTC"),
                "audit-proposal:create",
                proposal_id="audit-proposal",
            )
    assert await workspace.list(who) == []
    assert await reminders.list(who) == []
    if original_audit is not None:
        monkeypatch.setattr(workspace.store, "_audit", original_audit)
    else:
        async with control._pool.acquire() as conn:
            await conn.execute(f"""DROP TRIGGER reject_proposal_audit ON {ns.table("audit")}""")
    await workspace.reserve_proposal(who, "audit-proposal", "cancel", {})


@pytest.mark.parametrize("domain", ["native", "reminder", "cancel"])
async def test_proposal_receipt_survives_missing_terminal_message(env, domain):
    """领域已提交但终态聊天缺失时，权威结果仍能恢复且不再待确认。"""
    from xuanpu_server.reminders.models import ReminderInput

    workspace, who, _, _ = env
    proposal = {"route": {"workspace_draft": {"kind": "todo", "data": {"title": "原始草稿"}}}}
    await workspace.append_message(who, "assistant", "待确认", "crash:assistant", metadata=proposal)
    assert await workspace.proposal_result(who, "crash") is None
    if domain == "native":
        item = await workspace.create(
            who, "todo", {"title": "确认前已修改"}, "crash:create", proposal_id="crash"
        )
        expected = {"state": "confirmed", "result": item}
        with pytest.raises(ConflictError):
            await workspace.create(
                who, "todo", {"title": "原始草稿"}, "crash:create", proposal_id="crash"
            )
    elif domain == "reminder":
        record = await reminder_service(env).create(
            who,
            ReminderInput(title="提醒", due_at=1500.0, timezone="UTC"),
            "crash:create",
            proposal_id="crash",
        )
        expected = {"state": "confirmed", "result": {"reminder_id": record.reminder_id}}
    else:
        await workspace.reserve_proposal(who, "crash", "cancel", {})
        expected = {"state": "cancelled"}
    assert await workspace.find_message(who, "crash:confirmed") is None
    assert await workspace.find_message(who, "crash:cancelled") is None
    assert await workspace.proposal_result(who, "crash") == expected
    assert await workspace.pending_proposals(who) == []
    assert await workspace.proposal_result(replace(who, user_id="usr_b"), "crash") is None


@pytest.mark.parametrize(
    "url", ["https://example.com:bad/a", "https://example.com:70000/a", "https://%/file"]
)
async def test_external_link_rejects_invalid_browser_url(env, url):
    """端口和主机名无效的地址在写入前拒绝。"""
    service, who, _, _ = env
    with pytest.raises(InvalidRequestError):
        await service.create(who, "file", {"name": "引用", "source_url": url}, "bad-url")


async def test_history_before_cursor_combines_bounds_and_matter(env):
    """最新历史窗口支持双游标和事项过滤，返回保持升序。"""
    service, who, _, _ = env
    matter = await service.create(who, "matter", {"title": "分页"}, "page-matter")
    for i in range(1, 7):
        await service.append_message(
            who, "user", f"消息{i}", f"page-{i}", matter_id=matter["id"] if i % 2 else None
        )
    assert [
        m["id"] for m in await service.messages(who, after_id=1, before_id=6, latest=True, limit=2)
    ] == [4, 5]
    assert [
        m["id"]
        for m in await service.messages(
            who, before_id=5, latest=True, limit=2, matter_id=matter["id"]
        )
    ] == [1, 3]
    with pytest.raises(InvalidRequestError):
        await service.messages(who, before_id=9223372036854775808)


async def test_route_claim_choice_and_outcome_are_durable_and_fenced(env):
    """请求指纹覆盖完整输入，租约到期恢复选择且隔离迟到写入。"""
    service, who, _, _ = env
    payload = {"text": "业务", "session_id": None, "context": [], "timezone": "UTC"}
    first = await service.claim_route(who, "routing", payload)
    with pytest.raises(ConflictError):
        await service.claim_route(who, "routing", payload)
    with pytest.raises(ConflictError):
        await service.claim_route(who, "routing", {**payload, "timezone": "Asia/Shanghai"})
    await service.save_route_choice(
        who,
        "routing",
        first["token"],
        {"package": "support-desk", "entry": "triage", "message": None},
        [{"package": "support-desk", "version": "1.0.0", "entry": "triage"}],
    )
    service._now = lambda: 1100.0
    second = await service.claim_route(who, "routing", payload)
    assert second["choice"]["package"] == "support-desk" and second["token"] != first["token"]
    with pytest.raises(ConflictError):
        await service.complete_route(who, "routing", first["token"], {"message": "迟到"})
    await service.complete_route(who, "routing", second["token"], {"session_id": "stable-session"})
    assert (await service.claim_route(who, "routing", payload))["outcome"] == {
        "session_id": "stable-session"
    }


async def test_factory_timezone_not_saved_as_browser_preference(env):
    """历史占位时区不是可用浏览器显示时区。"""
    service, who, _, _ = env
    with pytest.raises(InvalidRequestError):
        await service.update_settings(who, {"timezone": "Factory"}, 1)
