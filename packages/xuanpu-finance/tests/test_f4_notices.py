"""真实PG验证原生通知派生、当前授权和人工已读。"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.storage import FinanceStore

from .test_expense_domain import invoice, setup, submitted
from .test_f3_domain import configured
from .test_f3_domain import invoice as f3_invoice


async def test_native_notice_consumes_real_outbox_and_current_approver(store: FinanceStore) -> None:
    """实际提交生成本人状态与当前审批通知，等待中的下一人不可见。"""
    owner, approvers = await setup(store, two=True)
    await invoice(store)
    doc = await submitted(store, owner)
    assert hasattr(store, "refresh_finance_notifications"), "缺少持久站内通知消费者"
    await store.refresh_finance_notifications()
    mine = await store.list_finance_notifications(owner, "e")
    pending = await store.list_finance_notifications(approvers[0], "e")
    assert mine["total"] == 1
    assert pending["total"] == 1
    assert pending["items"][0]["target_id"] == doc["id"]
    assert pending["items"][0]["kind"] == "approval_pending"
    assert (await store.list_finance_notifications(approvers[1], "e"))["total"] == 0
    assert await store.pool.fetchval("SELECT status FROM finance_outbox") == "inbox_recorded"
    assert set(mine["items"][0]) == {
        "id",
        "legal_entity_id",
        "version",
        "kind",
        "title",
        "target_type",
        "target_id",
        "created_at",
        "read_at",
        "source_status",
    }


async def test_notice_restart_concurrency_read_cas_and_revocation(store: FinanceStore) -> None:
    """并发刷新稳定去重，已读需真人/CAS/幂等且撤权后不可重放。"""
    owner, approvers = await setup(store)
    await invoice(store)
    await submitted(store, owner)
    assert hasattr(store, "refresh_finance_notifications"), "缺少持久站内通知消费者"
    await asyncio.gather(*(store.refresh_finance_notifications() for _ in range(3)))
    restarted = FinanceStore(store.pool)
    assert await restarted.refresh_finance_notifications() == 0
    notice = (await store.list_finance_notifications(approvers[0], "e"))["items"][0]
    with pytest.raises(FinanceError) as agent_error:
        await store.read_finance_notification(
            replace(approvers[0], origin="agent"), notice["id"], 1, "read"
        )
    assert agent_error.value.code == "HUMAN_REQUIRED"
    result = await store.read_finance_notification(approvers[0], notice["id"], 1, "read")
    assert result["version"] == 2 and result["read_at"] is not None
    assert (
        await restarted.read_finance_notification(approvers[0], notice["id"], 1, "read") == result
    )
    with pytest.raises(FinanceError) as conflict:
        await store.read_finance_notification(approvers[0], notice["id"], 1, "different")
    assert conflict.value.code == "VERSION_CONFLICT"
    assert (await store.list_finance_notifications(approvers[0], "e"))["unread_total"] == 0
    await store.grant("t", "a", "e", "collector")
    assert (await store.list_finance_notifications(approvers[0], "e"))["total"] == 0
    with pytest.raises(FinanceError):
        await store.read_finance_notification(approvers[0], notice["id"], 1, "read")
    await store.grant("t", "a", "e", "approver")
    assert (await store.list_finance_notifications(approvers[0], "e"))["total"] == 0


async def test_notice_cursor_scope_count_and_bounded_progress(store: FinanceStore) -> None:
    """batch和分页均有界，独立计数，跨主体及过滤游标被拒绝。"""
    owner, _ = await setup(store)
    for i in range(4):
        await store.pool.execute(
            "INSERT INTO finance_business_operation VALUES('t','e',$1,'owner',NULL,'{\"resources\":[]}'::jsonb,now())",
            f"bad-{i}",
        )
        await store.pool.execute(
            "INSERT INTO finance_outbox(tenant_id,legal_entity_id,id,operation_id,data) VALUES('t','e',$1,$1,'{}')",
            f"bad-{i}",
        )
    await invoice(store)
    await submitted(store, owner)
    assert hasattr(store, "refresh_finance_notifications"), "缺少持久站内通知消费者"
    for _ in range(10):
        assert await store.refresh_finance_notifications(batch_size=1) <= 1
    page = await store.list_finance_notifications(owner, "e", limit=1)
    assert page["total"] == 1
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_outbox WHERE status='unmapped_source'"
        )
        == 4
    )
    # 多条真实来源用于检验跨页游标及未读总数。
    for i in range(3):
        await store.pool.execute(
            "INSERT INTO finance_outbox(tenant_id,legal_entity_id,id,operation_id,data) SELECT tenant_id,legal_entity_id,$1,operation_id,data FROM finance_outbox WHERE status='inbox_recorded' LIMIT 1",
            f"extra-{i}",
        )
    await store.refresh_finance_notifications()
    page = await store.list_finance_notifications(owner, "e", limit=1)
    assert page["total"] == page["unread_total"] == 4 and page["next_cursor"]
    assert (await store.list_finance_notifications(owner, "e", page["next_cursor"], 1))[
        "total"
    ] == 4
    for principal, unread in [(FinancePrincipal("t", "reviewer"), False), (owner, True)]:
        with pytest.raises(FinanceError) as error:
            await store.list_finance_notifications(principal, "e", page["next_cursor"], 1, unread)
        assert error.value.code == "CURSOR_INVALID"


async def test_f3_unknown_uses_real_operation_without_expense_parent(store: FinanceStore) -> None:
    """独立F3未知通知引用原任务，无报销父单也能恢复。"""
    owner, _, _ = await configured(store)
    await f3_invoice(store)
    op = await store.verify_invoice(owner, "blue", 1, "verify")
    job = await store.claim_compliance()
    assert job is not None
    await store.prepare_compliance_dispatch(job)
    await store.defer_compliance(job, "TIMEOUT", uncertain=True)
    await store.release_compliance(job)
    assert hasattr(store, "refresh_finance_notifications"), "缺少持久站内通知消费者"
    await store.refresh_finance_notifications()
    notices = await store.list_finance_notifications(owner, "e")
    assert notices["total"] == 1
    assert notices["items"][0]["target_type"] == "operation"
    assert notices["items"][0]["target_id"] == op["id"]
    assert notices["items"][0]["source_status"] == "outcome_unknown"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_expense_claim") == 0


async def test_f0_exception_owner_and_current_version_are_not_broadcast(
    store: FinanceStore,
) -> None:
    """F0开放异常仅发给原owner，解决后旧提醒不可继续读。"""
    from .test_f3_archive_domain import originals

    owner, _, _ = await configured(store)
    await f3_invoice(store)
    await originals(store)
    await store.pool.execute(
        "INSERT INTO finance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,item_position,rule_id) VALUES('t','e','ex','owner','import-blue',0,'needs_material')"
    )
    await store.refresh_finance_notifications()
    result = await store.list_finance_notifications(owner, "e")
    assert result["total"] == 1 and result["items"][0]["target_id"] == "ex"
    assert (await store.list_finance_notifications(FinancePrincipal("t", "tax"), "e"))["total"] == 0
    await store.pool.execute(
        "UPDATE finance_exception SET status='resolved',version=version+1 WHERE id='ex'"
    )
    assert (await store.list_finance_notifications(owner, "e"))["total"] == 0


async def test_f2_approval_and_unknown_keep_original_plan_and_operation(
    store: FinanceStore,
) -> None:
    """F2审批通知恢复原计划，未知通知只恢复原经办的真实任务。"""
    from xuanpu_finance.expenses.models import ActionExecution, ActionPreview

    from .test_expense_domain import execute
    from .test_f2_domain import approved_payable, ref

    _, payer, _, approver, doc = await approved_payable(store)
    plan = await store.preview_finance_action(
        payer,
        ActionPreview(
            action="execute_payment",
            target_type="payable",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"amount": "1000.00", "payee_ref": ref("supplier_account", "account")},
        ),
        "payment-plan",
    )
    await store.refresh_finance_notifications()
    queue = await store.list_finance_notifications(approver, "e")
    assert any(
        n["target_type"] == "action_plan" and n["target_id"] == plan["id"] for n in queue["items"]
    )
    item = next(
        i
        for i in (await store.list_review_queue(approver, "e"))["items"]
        if i["target_id"] == plan["id"]
    )
    decision = await store.preview_finance_action(
        approver,
        ActionPreview(
            action="approve",
            target_type="action_plan",
            target_id=plan["id"],
            expected_version=1,
            parameters={"workflow_step_id": item["workflow_step_id"]},
        ),
        "decision",
    )
    await execute(store, approver, decision)
    op = await store.execute_finance_action(
        payer, ActionExecution(plan_id=plan["id"], plan_digest=plan["digest"]), "execute"
    )
    job = await store.claim_settlement()
    assert job is not None
    await store.prepare_settlement_dispatch(job)
    await store.defer_settlement(job, "TIMEOUT", uncertain=True)
    await store.release_settlement(job)
    await store.refresh_finance_notifications()
    result = await store.list_finance_notifications(payer, "e")
    assert any(
        n["kind"] == "outcome_unknown" and n["target_id"] == op["id"] for n in result["items"]
    )


async def test_f3_pending_expiry_epoch_and_executor_only_deadline(store: FinanceStore) -> None:
    """F3真实审批代际及72小时期限独立于费用父单且无全员广播。"""
    import json
    from datetime import UTC, datetime, timedelta

    from .test_f3_archive_domain import archive_plan, originals
    from .test_f3_guards import verified

    owner, tax, auditor = await verified(store)
    await originals(store)
    plan = await archive_plan(store, tax)
    await store.refresh_finance_notifications()
    result = await store.list_finance_notifications(auditor, "e")
    assert result["total"] == 1 and result["items"][0]["target_id"] == plan["id"]
    # 计划不可变，数据库时钟之外用真实短期限的新计划验证逾期投影。
    await store.pool.execute(
        "INSERT INTO finance_compliance_plan(tenant_id,legal_entity_id,id,actor_id,owner_id,target_type,target_id,target_version,digest,expires_at,data) SELECT tenant_id,legal_entity_id,'expired-plan',actor_id,owner_id,target_type,target_id,target_version,digest,now()-interval '1 hour',data FROM finance_compliance_plan WHERE id=$1",
        plan["id"],
    )
    await store.pool.execute(
        "INSERT INTO finance_compliance_workflow VALUES('t','e','expired-workflow','expired-plan',1,'under_review')"
    )
    await store.pool.execute(
        "INSERT INTO finance_compliance_workflow_step SELECT tenant_id,legal_entity_id,'expired-step','expired-workflow',position,approver_user_id,authority_generation,status,version FROM finance_compliance_workflow_step LIMIT 1"
    )
    now = datetime.now(UTC)
    data = {
        "initiated_at": (now - timedelta(hours=73)).isoformat(),
        "authority_deadline": (now + timedelta(hours=20)).isoformat(),
    }
    await store.pool.execute(
        "INSERT INTO finance_red_letter_case(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_case_id,authority_revision,status,data) VALUES('t','e','red-case','owner','blue','external-red',1,'awaiting_confirmation',$1::jsonb)",
        json.dumps(data),
    )
    await store.refresh_finance_notifications()
    assert any(
        n["kind"] == "approval_overdue"
        for n in (await store.list_finance_notifications(auditor, "e"))["items"]
    )
    deadlines = (await store.list_finance_notifications(tax, "e"))["items"]
    assert any(
        n["kind"] == "red_deadline" and n["source_status"] == "expired_pending_check"
        for n in deadlines
    )
    assert (await store.list_finance_notifications(owner, "e"))["total"] == 0
    await store.grant("t", "audit", "e", "collector")
    await store.grant("t", "audit", "e", "approver")
    assert (await store.list_finance_notifications(auditor, "e"))["total"] == 0


async def test_archive_health_and_new_event_remind_only_current_executor(
    store: FinanceStore,
) -> None:
    """真实封存后健康失败及新增事实分别稳定提醒当前有权经办。"""
    from .test_f3_archive_domain import accept_archive, originals, outcome_for, save_archive_result
    from .test_f3_domain import fresh_snapshot, result_for
    from .test_f3_guards import verified

    owner, tax, auditor = await verified(store)
    await originals(store)
    job, request = await accept_archive(store, tax, auditor)
    await save_archive_result(store, job, request, outcome_for(request))
    await store.refresh_finance_notifications()
    assert (await store.list_finance_notifications(tax, "e"))["total"] == 0
    archive = (await store.list_archives(tax, "e"))["items"][0]
    await store.pool.execute(
        "UPDATE finance_archive_health SET last_audit_status='failed' WHERE archive_id=$1",
        archive["id"],
    )
    await store.refresh_finance_notifications()
    result = await store.list_finance_notifications(tax, "e")
    assert result["total"] == 1 and result["items"][0]["kind"] == "archive_attention"
    assert (await store.list_finance_notifications(owner, "e"))["total"] == 0
    await store.verify_invoice(owner, "blue", 2, "new")
    verify = await store.claim_compliance()
    assert verify is not None
    await result_for(store, verify, fresh_snapshot(2, verification_id="after-archive"))
    await store.refresh_finance_notifications()
    current = await store.list_finance_notifications(tax, "e")
    assert current["total"] == 1 and current["items"][0]["id"] != result["items"][0]["id"]
    assert await store.refresh_finance_notifications() == 0


async def test_revoked_approval_cannot_read_old_operation_notice(store: FinanceStore) -> None:
    """原审批操作仍有本人operation，不得在失去目标读取权后从通知泄露。"""
    from .test_expense_domain import execute, preview

    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    step = (await store.list_review_queue(people[0], "e"))["items"][0]
    plan = await preview(
        store, people[0], doc, action="approve", workflow_step_id=step["workflow_step_id"]
    )
    await execute(store, people[0], plan)
    await store.refresh_finance_notifications()
    assert (await store.list_finance_notifications(people[0], "e"))["total"] == 1
    await store.grant("t", "a", "e", "collector")
    assert (await store.list_finance_notifications(people[0], "e"))["total"] == 0


async def test_archive_recovery_hides_old_attention_and_repeat_failure_is_new(
    store: FinanceStore,
) -> None:
    """真实审计失败→恢复→再次失败分别生成当前可操作的未读提醒。"""
    import json

    from .test_f3_archive_domain import accept_archive, originals, outcome_for, save_archive_result
    from .test_f3_guards import verified

    _, tax, auditor = await verified(store)
    await originals(store)
    job, original = await accept_archive(store, tax, auditor)
    await save_archive_result(store, job, original, outcome_for(original))
    archive = (await store.list_archives(tax, "e"))["items"][0]
    notice_id = None
    for turn, success in enumerate([False, True, False]):
        await store.audit_archive(tax, archive["id"], 1, f"audit-{turn}")
        audit = await store.claim_compliance()
        assert audit is not None
        request = await store.prepare_compliance_dispatch(audit)
        response = {
            **{k: v for k, v in request.items() if k != "payload"},
            "status": "succeeded" if success else "failed",
            "evidence": [
                {
                    "id": "proof",
                    "sha256": "f" * 64,
                    "media_type": "application/json",
                    "object_ref": "archive-proof",
                }
            ],
            "outcome": outcome_for(original) if success else None,
        }
        await store.record_compliance_result(audit, response, json.dumps(response).encode())
        await store.release_compliance(audit)
        await store.refresh_finance_notifications()
        result = await store.list_finance_notifications(tax, "e", unread_only=True)
        if success:
            assert result["total"] == result["unread_total"] == 0
            assert (await store.list_finance_notifications(tax, "e"))["total"] == 0
        else:
            assert result["total"] == 1
            notice = result["items"][0]
            assert notice["id"] != notice_id
            notice_id = notice["id"]
            await store.read_finance_notification(tax, notice_id, 1, f"read-{turn}")


async def test_archive_supplement_clears_current_notice_without_erasing_history(
    store: FinanceStore,
) -> None:
    """真实补包完成后旧待办退出当前未读，原通知和封存历史保留。"""
    from xuanpu_finance.expenses.models import ActionExecution

    from .test_f3_archive_domain import (
        accept_archive,
        archive_plan,
        originals,
        outcome_for,
        save_archive_result,
    )
    from .test_f3_guards import approve, verified

    _, tax, auditor = await verified(store)
    await originals(store)
    job, original = await accept_archive(store, tax, auditor)
    await save_archive_result(store, job, original, outcome_for(original))
    archive = (await store.list_archives(tax, "e"))["items"][0]
    await store.pool.execute(
        "INSERT INTO finance_event(tenant_id,legal_entity_id,aggregate_id,kind,data) VALUES('t','e','blue','settlement.succeeded','{}')"
    )
    await store.refresh_finance_notifications()
    result = await store.list_finance_notifications(tax, "e")
    assert result["total"] == result["unread_total"] == 1
    archive = await store.get_archive(tax, archive["id"])
    plan = await archive_plan(
        store, tax, parent=archive["manifest_hash"], events=archive["supplement_event_ids"]
    )
    await approve(store, auditor, plan)
    await store.execute_finance_action(
        tax, ActionExecution(plan_id=plan["id"], plan_digest=plan["digest"]), "supplement-execute"
    )
    supplement = await store.claim_compliance()
    assert supplement is not None
    request = await store.prepare_compliance_dispatch(supplement)
    outcome = outcome_for(request)
    outcome["manifest_hash"] = "e" * 64
    outcome["members"][-2]["sha256"] = "e" * 64
    await save_archive_result(store, supplement, request, outcome)
    # 读取即时复核真实事件，甚至不需要等下一次通知扫描。
    result = await store.list_finance_notifications(tax, "e")
    assert result["total"] == result["unread_total"] == 0
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_notification WHERE kind='archive_attention'"
        )
        == 1
    )


async def test_payable_approval_notice_keeps_real_target_kind(store: FinanceStore) -> None:
    """应付待审提醒不能伪装报销，否则人工入口会读取错误的单据API。"""
    from xuanpu_finance.expenses.models import ActionPreview, AllocationRequest

    from .test_expense_domain import execute
    from .test_f2_domain import configured as settlement_configured
    from .test_f2_domain import payable
    owner,_,_,approver=await settlement_configured(store)
    await invoice(store)
    doc=await payable(store,owner)
    await store.create_invoice_allocation(owner,AllocationRequest(invoice_id='i',invoice_version=1,document_id=doc['id'],document_kind='payable',expected_document_version=doc['version'],amount='1000.00',tax='0.00'),'allocation')
    doc=await store.get_payable(owner,doc['id'])
    plan=await store.preview_finance_action(owner,ActionPreview(action='submit_payable',target_type='payable',target_id=doc['id'],expected_version=doc['version'],parameters={}),'submit')
    await execute(store,owner,plan)
    await store.refresh_finance_notifications()
    pending=await store.list_finance_notifications(approver,'e')
    assert any(n['target_type']=='payable' and n['target_id']==doc['id'] for n in pending['items'])
