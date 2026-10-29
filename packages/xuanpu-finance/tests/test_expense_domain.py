"""独立真实PG上的报销占额、顺序审批及回滚验收。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import (
    ActionExecution,
    ActionPreview,
    AllocationRequest,
    ExceptionResolution,
    ExpenseDraft,
    ExpensePolicy,
    ExpenseUpdate,
)
from xuanpu_finance.models import FinancePrincipal
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage import FinanceStore


async def setup(
    store: FinanceStore, *, two: bool = False
) -> tuple[FinancePrincipal, list[FinancePrincipal]]:
    """提供显式公司制度和真实审批人授权。"""
    await store.seed_company("t", "e", "公司", "BUYER")
    owner = FinancePrincipal("t", "owner", origin="human")
    await store.grant("t", "owner", "e")
    people = [
        FinancePrincipal("t", name, origin="human") for name in (("a", "b") if two else ("a",))
    ]
    for person in people:
        await store.grant("t", person.user_id, "e", "approver")
    await store.grant("t", "reviewer", "e", "reviewer")
    await store.configure_expenses([policy(two=two)])
    return owner, people


def policy(
    *, two: bool = False, version: int = 1, enabled: bool = True, minimum: str = "0.00"
) -> ExpensePolicy:
    """构造已审批的开发制度样例，明确不要求官方查验。"""
    return ExpensePolicy(
        tenant_id="t",
        legal_entity_id="e",
        version=version,
        enabled=enabled,
        categories=[{"id": "travel", "name": "差旅"}],
        cost_centers=[],
        steps=[
            {"id": name, "name": name, "approver_user_id": name, "minimum_amount": minimum}
            for name in (("a", "b") if two else ("a",))
        ],
        exception_owner_user_id="reviewer",
        require_verified_invoice=False,
    )


async def invoice(
    store: FinanceStore,
    id: str = "i",
    gross: str = "1000.00",
    tax: str = "100.00",
    *,
    line: bool = False,
) -> None:
    """直接写入等价F0解析快照以隔离领域行为，保留未查验语义。"""
    data = {
        "id": id,
        "version": 1,
        "legal_entity_id": "e",
        "gross": gross,
        "tax": tax,
        "buyer_tax_id": "BUYER",
        "is_red": False,
        "ticket_status": "normal",
        "verification_status": "not_checked",
        "authority_status": "unverified",
        "available_amount": gross,
    }
    await store.pool.execute(
        "INSERT INTO finance_invoice(tenant_id,legal_entity_id,id,owner_id,seller_tax_id,number,issue_date,data,canonical_digest) VALUES('t','e',$1,'owner','SELLER',$1,'2026-10-21',$2::jsonb,'digest')",
        id,
        json.dumps(data),
    )
    if line:
        await store.pool.execute(
            "INSERT INTO finance_invoice_line VALUES('t','e',$1,0,$2::jsonb)",
            id,
            json.dumps({"id": id + "-line", "amount": "500.00", "tax": "50.00"}),
        )


async def claim(
    store: FinanceStore, owner: FinancePrincipal, amount: str = "600.00", key: str = "c"
) -> dict[str, Any]:
    """创建受控分类的本人报销。"""
    return await store.create_expense_claim(
        owner,
        ExpenseDraft(
            legal_entity_id="e",
            reason="出差",
            lines=[
                {
                    "category_id": "travel",
                    "amount": amount,
                    "occurred_on": "2026-10-21",
                    "reason": "交通",
                }
            ],
        ),
        key,
    )


async def allocate(
    store: FinanceStore,
    owner: FinancePrincipal,
    doc: dict[str, Any],
    id: str = "i",
    amount: str | None = None,
    tax: str = "0.00",
    line: str | None = None,
) -> dict[str, Any]:
    """按最新单据版本创建提议，调用方读取服务端递增版本。"""
    return await store.create_invoice_allocation(
        owner,
        AllocationRequest(
            invoice_id=id,
            invoice_version=1,
            invoice_line_id=line,
            document_id=doc["id"],
            document_kind="expense_claim",
            expected_document_version=doc["version"],
            amount=amount or doc["total"],
            tax=tax,
        ),
        "alloc-" + doc["id"] + id + tax,
    )


async def preview(
    store: FinanceStore,
    who: FinancePrincipal,
    doc: dict[str, Any],
    action: str = "submit_claim",
    **parameters: Any,
) -> dict[str, Any]:
    """读取最新快照后显式预检，预检不占额。"""
    return await store.preview_finance_action(
        who,
        ActionPreview(
            action=action,
            target_type="expense_claim",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters=parameters,
        ),
        "p-" + doc["id"] + action + str(doc["version"]) + request_digest(parameters)[:16],
    )


async def execute(
    store: FinanceStore, who: FinancePrincipal, plan: dict[str, Any], key: str | None = None
) -> dict[str, Any]:
    """仅传不可变计划和摘要，业务决定必须来自人工。"""
    return await store.execute_finance_action(
        who,
        ActionExecution(plan_id=plan["id"], plan_digest=plan["digest"]),
        key or "x-" + plan["id"],
    )


async def submitted(
    store: FinanceStore, owner: FinancePrincipal, amount: str = "600.00", key: str = "c"
) -> dict[str, Any]:
    """建立待审单据。"""
    doc = await claim(store, owner, amount, key)
    await allocate(store, owner, doc)
    doc = await store.get_expense_claim(owner, doc["id"])
    plan = await preview(store, owner, doc)
    assert plan["executable"], plan["blockers"]
    await execute(store, owner, plan)
    return await store.get_expense_claim(owner, doc["id"])


async def test_two_600_claims_race_for_1000_with_atomic_reservation(store: FinanceStore) -> None:
    """并发预检均可，执行最多一单占额，失败操作无半提交。"""
    owner, _ = await setup(store)
    await invoice(store)
    docs = [await claim(store, owner, key=key) for key in ("c1", "c2")]
    for doc in docs:
        await allocate(store, owner, doc)
    plans = [
        await preview(store, owner, await store.get_expense_claim(owner, doc["id"])) for doc in docs
    ]
    assert all(plan["executable"] for plan in plans)
    assert (await store.get_invoice(owner, "i"))["available_amount"] == "1000.00"
    outcomes = await asyncio.gather(
        *(execute(store, owner, plan) for plan in plans), return_exceptions=True
    )
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, FinanceError) for outcome in outcomes) == 1
    assert (await store.get_invoice(owner, "i"))["available_amount"] == "400.00"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_workflow") == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_business_operation") == 1


async def test_multi_invoice_insufficient_rolls_back_entire_claim(store: FinanceStore) -> None:
    """多票任一不足时全部proposal/事件/工作流保持执行前。"""
    owner, _ = await setup(store)
    await invoice(store)
    await invoice(store, "j", "100.00", "0.00")
    first = await submitted(store, owner)
    assert first["status"] == "under_review"
    doc = await claim(store, owner, "600.00", "other")
    await allocate(store, owner, doc, amount="500.00")
    doc = await store.get_expense_claim(owner, doc["id"])
    await allocate(store, owner, doc, "j", "100.00")
    plan = await preview(store, owner, await store.get_expense_claim(owner, doc["id"]))
    assert not plan["executable"]
    with pytest.raises(FinanceError):
        await execute(store, owner, plan)
    assert (await store.get_invoice(owner, "j"))["available_amount"] == "100.00"
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_allocation WHERE document_id=$1 AND state='proposed'",
            doc["id"],
        )
        == 2
    )


async def test_two_step_human_approval_survives_restart_and_freezes_claim_version(
    store: FinanceStore,
) -> None:
    """中间批准不改claim版本，最终批准保留reserved且grant仅报销批准。"""
    owner, reviewers = await setup(store, two=True)
    await invoice(store)
    doc = await submitted(store, owner)
    initial_version = doc["version"]
    for index, reviewer in enumerate(reviewers):
        store = FinanceStore(store.pool)
        queue = await store.list_review_queue(reviewer, "e")
        assert queue["total"] == 1
        step = queue["items"][0]
        plan = await preview(
            store, reviewer, doc, "approve", workflow_step_id=step["workflow_step_id"]
        )
        with pytest.raises(FinanceError, match="HUMAN_REQUIRED"):
            await execute(store, replace(reviewer, origin="agent"), plan)
        op = await execute(store, reviewer, plan)
        assert op["status"] == "succeeded" and op["items"] == []
        assert await execute(store, reviewer, plan, "new-key") == op
        assert await store.get_operation(reviewer, op["id"]) == op
        doc = await store.get_expense_claim(owner, doc["id"])
        assert doc["version"] == initial_version + int(index == 1)
    assert doc["status"] == "approved"
    assert (await store.get_invoice(owner, "i"))["available_amount"] == "400.00"
    assert (
        await store.pool.fetchval("SELECT count(*) FROM finance_allocation WHERE state='consumed'")
        == 0
    )
    assert (
        await store.pool.fetchval("SELECT scope FROM finance_execution_grant") == "expense_approved"
    )
    assert await store.pool.fetchval("SELECT count(*) FROM finance_decision") == 2


@pytest.mark.parametrize("action", ["reject", "withdraw"])
async def test_reject_withdraw_release_once_and_cancel_steps(
    store: FinanceStore, action: str
) -> None:
    """驳回/撤回一次释放，历史决定不可丢失。"""
    owner, reviewers = await setup(store, two=True)
    await invoice(store)
    doc = await submitted(store, owner)
    who = reviewers[0] if action == "reject" else owner
    params = {"reason": "需要补正"}
    if action == "reject":
        params["workflow_step_id"] = (await store.list_review_queue(who, "e"))["items"][0][
            "workflow_step_id"
        ]
    plan = await preview(store, who, doc, action, **params)
    op = await execute(store, who, plan)
    assert await execute(store, who, plan, "repeat") == op
    assert (await store.get_expense_claim(owner, doc["id"]))["status"] == (
        "rejected" if action == "reject" else "withdrawn"
    )
    assert (await store.get_invoice(owner, "i"))["available_amount"] == "1000.00"
    assert (await store.list_review_queue(reviewers[1], "e"))["total"] == 0


async def test_stale_policy_and_revoked_approver_block_but_owner_can_withdraw(
    store: FinanceStore,
) -> None:
    """关闭或升级制度后旧计划不可执行，owner使用新撤回计划清理历史。"""
    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    step = (await store.list_review_queue(people[0], "e"))["items"][0]
    old = await preview(store, people[0], doc, "approve", workflow_step_id=step["workflow_step_id"])
    await store.configure_expenses([policy(version=2, enabled=False)])
    assert not (await store.get_action_plan(people[0], old["id"]))["executable"]
    with pytest.raises(FinanceError):
        await execute(store, people[0], old)
    await store.pool.execute("DELETE FROM finance_grant WHERE user_id='a'")
    new = await preview(store, owner, doc, "withdraw", reason="制度已撤销")
    assert new["executable"], new["blockers"]
    await execute(store, owner, new)
    assert (await store.get_invoice(owner, "i"))["available_amount"] == "1000.00"


async def test_cumulative_tax_and_line_amount_cannot_exceed_invoice(store: FinanceStore) -> None:
    """提议叠加税额和指定行占额不能依赖单次创建校验。"""
    owner, _ = await setup(store)
    await invoice(store, line=True)
    doc = await claim(store, owner, "600.00")
    await allocate(store, owner, doc, amount="300.00", tax="30.00", line="i-line")
    doc = await store.get_expense_claim(owner, doc["id"])
    await allocate(store, owner, doc, amount="300.00", tax="31.00", line="i-line")
    plan = await preview(store, owner, await store.get_expense_claim(owner, doc["id"]))
    assert not plan["executable"]
    with pytest.raises(FinanceError):
        await execute(store, owner, plan)
    assert (
        await store.pool.fetchval("SELECT count(*) FROM finance_allocation WHERE state='reserved'")
        == 0
    )


async def test_wrong_person_self_review_and_isolation(store: FinanceStore) -> None:
    """reviewer读权不是审批权，approver只看实际分派及关联票据。"""
    owner, people = await setup(store, two=True)
    await invoice(store)
    doc = await submitted(store, owner)
    step = (await store.list_review_queue(people[0], "e"))["items"][0]
    for who in (owner, FinancePrincipal("t", "reviewer", origin="human")):
        plan = await preview(store, who, doc, "approve", workflow_step_id=step["workflow_step_id"])
        assert not plan["executable"]
        with pytest.raises(FinanceError):
            await execute(store, who, plan)
    with pytest.raises(FinanceError):
        await store.get_expense_claim(FinancePrincipal("other", "owner"), doc["id"])
    assert (await store.get_invoice(people[0], "i"))["id"] == "i"
    draft = await claim(store, owner, key="draft")
    with pytest.raises(FinanceError):
        await store.get_expense_claim(people[0], draft["id"])
    assert (await store.list_expense_claims(people[0], "e"))["total"] == 1


async def test_draft_revision_releases_proposals_and_idempotency_conflicts(
    store: FinanceStore,
) -> None:
    """更新仅本人且保留历史，幂等键异内容拒绝。"""
    owner, _ = await setup(store)
    await invoice(store)
    doc = await claim(store, owner)
    allocation = await allocate(store, owner, doc)
    current = await store.get_expense_claim(owner, doc["id"])
    updated = await store.update_expense_claim(
        owner,
        doc["id"],
        ExpenseUpdate(
            expected_version=current["version"], draft=ExpenseDraft(**current["content"])
        ),
        "update",
    )
    assert updated["version"] == current["version"] + 1
    assert (await store.get_invoice_allocation(owner, allocation["id"]))["state"] == "released"
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_expense_history WHERE id=$1", doc["id"]
        )
        == 3
    )
    with pytest.raises(FinanceError, match="IDEMPOTENCY_CONFLICT"):
        await claim(store, owner, "500.00")
    assert (await store.list_expense_categories(owner, "e"))["items"] == [
        {"id": "travel", "name": "差旅", "policy_version": 1}
    ]


async def test_event_failure_rolls_back_business_operation_and_reservation(
    store: FinanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件写入故障不能留下额度、工作流或成功回执。"""
    owner, _ = await setup(store)
    await invoice(store)
    doc = await claim(store, owner)
    await allocate(store, owner, doc)
    current = await store.get_expense_claim(owner, doc["id"])
    plan = await preview(store, owner, current)

    async def fail(*args: Any, **kwargs: Any) -> None:
        """模拟数据库同事务事件失败。"""
        raise RuntimeError("event failed")

    monkeypatch.setattr(store, "_event", fail)
    with pytest.raises(RuntimeError, match="event failed"):
        await execute(store, owner, plan)
    assert (await store.get_expense_claim(owner, doc["id"])) == current
    assert await store.pool.fetchval("SELECT count(*) FROM finance_workflow") == 0
    assert await store.pool.fetchval("SELECT count(*) FROM finance_business_operation") == 0
    assert (await store.get_invoice(owner, "i"))["available_amount"] == "1000.00"


async def test_empty_applicable_chain_and_immutable_policy_versions(store: FinanceStore) -> None:
    """门槛筛选为空不得自动批准，同版本异正文必须拒改。"""
    owner, _ = await setup(store)
    with pytest.raises(FinanceError, match="POLICY_VERSION_CONFLICT"):
        await store.configure_expenses([policy(minimum="1000.00")])
    await store.configure_expenses([policy(version=2, minimum="1000.00")])
    await invoice(store)
    doc = await claim(store, owner)
    await allocate(store, owner, doc)
    plan = await preview(store, owner, await store.get_expense_claim(owner, doc["id"]))
    assert not plan["executable"]
    with pytest.raises(FinanceError):
        await execute(store, owner, plan)


async def test_internal_record_without_invoice_and_frozen_mode(store: FinanceStore) -> None:
    """显式内部台账允许无结构化票据，但模式随版本冻结不能被新制度重标。"""
    owner, people = await setup(store)
    light = policy(version=2).model_dump()
    light["record_mode"] = "internal_record"
    await store.configure_expenses([ExpensePolicy(**light)])
    doc = await claim(store, owner)
    assert doc["record_mode"] == "internal_record"
    plan = await preview(store, owner, doc)
    assert plan["executable"], plan["blockers"]
    assert any("内部记录" in effect for effect in plan["effects"])
    await execute(store, owner, plan)
    doc = await store.get_expense_claim(owner, doc["id"])
    item = (await store.list_review_queue(people[0], "e"))["items"][0]
    approved = await preview(
        store, people[0], doc, "approve", workflow_step_id=item["workflow_step_id"]
    )
    await execute(store, people[0], approved)
    await store.configure_expenses([policy(version=3)])
    current = await store.get_expense_claim(owner, doc["id"])
    assert current["record_mode"] == "internal_record" and current["status"] == "approved"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 0


async def test_internal_mode_with_any_proposal_still_requires_exact_total(
    store: FinanceStore,
) -> None:
    """内部模式不放宽已关联票据的原子分配和税额边界。"""
    owner, _ = await setup(store)
    light = policy(version=2).model_dump()
    light["record_mode"] = "internal_record"
    await store.configure_expenses([ExpensePolicy(**light)])
    await invoice(store)
    doc = await claim(store, owner)
    await allocate(store, owner, doc, amount="100.00")
    plan = await preview(store, owner, await store.get_expense_claim(owner, doc["id"]))
    assert not plan["executable"]
    with pytest.raises(FinanceError, match="ALLOCATION_TOTAL_MISMATCH"):
        await execute(store, owner, plan)


async def receipt(store: FinanceStore, owner: FinancePrincipal, name: str = "receipt") -> str:
    """写入已保全但尚未解析成票据的F0真实责任回执。"""
    from xuanpu_finance.models import FileRef

    op = await store.accept_import(
        owner, "e", [FileRef(file_id=name, revision=1, sha256="0" * 64)], "file-" + name
    )
    await store.pool.execute(
        "INSERT INTO finance_receipt(tenant_id,legal_entity_id,id,owner_id,operation_id,position,object_key,sha256,file_ref) VALUES('t','e',$1,$2,$3,0,'object','0000000000000000000000000000000000000000000000000000000000000000',$4::jsonb)",
        name,
        owner.user_id,
        op["id"],
        json.dumps({"file_id": name, "revision": 1, "sha256": "0" * 64}),
    )
    return name


async def test_internal_attachments_are_actual_visible_same_company_receipts(
    store: FinanceStore,
) -> None:
    """PDF等回执可作内部材料，越权不存在和跨公司材料均拒绝且不伪造Invoice。"""
    owner, people = await setup(store)
    light = policy(version=2).model_dump()
    light["record_mode"] = "internal_record"
    await store.configure_expenses([ExpensePolicy(**light)])
    ref = await receipt(store, owner)
    template = {
        "legal_entity_id": "e",
        "reason": "内部记录",
        "lines": [
            {
                "category_id": "travel",
                "amount": "10.00",
                "occurred_on": "2026-10-21",
                "reason": "交通",
            }
        ],
        "evidence_ids": [ref],
    }
    doc = await store.create_expense_claim(owner, ExpenseDraft(**template), "attached")
    assert [entry["id"] for entry in doc["evidence"]] == [ref]
    assert doc["evidence"][0]["verification_state"] == "not_checked"
    assert await store.pool.fetchval("SELECT count(*) FROM finance_invoice") == 0
    await store.grant("t", "stranger", "e")
    foreign = await receipt(store, FinancePrincipal("t", "stranger"), "foreign")
    for ref_id in ("missing", foreign):
        with pytest.raises(FinanceError):
            await store.create_expense_claim(
                owner, ExpenseDraft(**{**template, "evidence_ids": [ref_id]}), "bad-" + ref_id
            )
    plan = await preview(store, owner, doc)
    await execute(store, owner, plan)
    current = await store.get_expense_claim(people[0], doc["id"])
    assert current["evidence"][0]["id"] == ref


async def test_hard_exception_can_only_attach_actual_visible_evidence(store: FinanceStore) -> None:
    """补充材料只追加证据，材料缺失仍须F0成功补件才能关闭。"""
    owner, _ = await setup(store)
    ref = await receipt(store, owner)
    operation = await store.pool.fetchval(
        "SELECT operation_id FROM finance_receipt WHERE id=$1", ref
    )
    await store.pool.execute(
        "INSERT INTO finance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,item_position,rule_id) VALUES('t','e','ex','owner',$1,0,'ORIGINAL_REQUIRED')",
        operation,
    )
    for disposition in ("resolve", "accept_warning"):
        with pytest.raises(FinanceError, match="HARD_BLOCK"):
            await store.resolve_exception(
                owner,
                "ex",
                ExceptionResolution(
                    expected_version=1, disposition=disposition, reason="已补充", evidence_ids=[ref]
                ),
                disposition,
            )
    with pytest.raises(FinanceError):
        await store.resolve_exception(
            owner,
            "ex",
            ExceptionResolution(
                expected_version=1,
                disposition="attach_evidence",
                reason="材料",
                evidence_ids=["missing"],
            ),
            "bad",
        )
    result = await store.resolve_exception(
        owner,
        "ex",
        ExceptionResolution(
            expected_version=1,
            disposition="attach_evidence",
            reason="补充真实材料",
            evidence_ids=[ref],
        ),
        "attach",
    )
    assert result["status"] == "investigating"
    assert result["version"] == 2 and result["rule_id"] == "ORIGINAL_REQUIRED"
    cases = await store.list_exceptions(owner, "e")
    assert cases["items"][0]["status"] == "investigating"
    assert cases["items"][0]["version"] == 2
    assert cases["items"][0]["evidence"][0]["id"] == ref
    assert await store.pool.fetchval("SELECT count(*) FROM finance_exception_decision") == 1


async def test_warning_requires_human_reviewer_reason_and_evidence(store: FinanceStore) -> None:
    """只有服务认可warning允许人工reviewer处置，agent不得升级。"""
    owner, _ = await setup(store)
    ref = await receipt(store, owner)
    operation = await store.pool.fetchval(
        "SELECT operation_id FROM finance_receipt WHERE id=$1", ref
    )
    await store.pool.execute(
        "INSERT INTO finance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,item_position,rule_id,severity) VALUES('t','e','warning','owner',$1,0,'MANUAL_REVIEW_WARNING','warning')",
        operation,
    )
    body = ExceptionResolution(
        expected_version=1,
        disposition="accept_warning",
        reason="核对后接受提醒",
        evidence_ids=[ref],
    )
    with pytest.raises(FinanceError):
        await store.resolve_exception(owner, "warning", body, "owner")
    with pytest.raises(FinanceError, match="HUMAN_REQUIRED"):
        await store.resolve_exception(FinancePrincipal("t", "reviewer"), "warning", body, "agent")
    await store.resolve_exception(
        FinancePrincipal("t", "reviewer", origin="human"), "warning", body, "human"
    )
    assert (await store.list_exceptions(owner, "e"))["items"][0]["status"] == "accepted"


async def test_approval_rechecks_invoice_versions_and_hard_ticket_state(
    store: FinanceStore,
) -> None:
    """审批前票据被作废或改版不能用新计划绕过原提交票证约束。"""
    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    step = (await store.list_review_queue(people[0], "e"))["items"][0]
    await store.pool.execute(
        "UPDATE finance_invoice SET data=jsonb_set(data,'{ticket_status}','\"voided\"') WHERE id='i'"
    )
    plan = await preview(
        store, people[0], doc, "approve", workflow_step_id=step["workflow_step_id"]
    )
    assert not plan["executable"]
    with pytest.raises(FinanceError):
        await execute(store, people[0], plan)


async def test_plan_expiry_digest_stale_and_mode_change_require_revision(
    store: FinanceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """摘要错误、过期、旧单据版本和记录模式变化均不可执行。"""
    from datetime import UTC, datetime, timedelta

    import xuanpu_finance.storage.business_plans as plans_module

    owner, _ = await setup(store)
    await invoice(store)
    doc = await claim(store, owner)
    allocation = await allocate(store, owner, doc)
    doc = await store.get_expense_claim(owner, doc["id"])
    plan = await preview(store, owner, doc)
    with pytest.raises(FinanceError, match="PLAN_DIGEST_MISMATCH"):
        await store.execute_finance_action(
            owner, ActionExecution(plan_id=plan["id"], plan_digest="0" * 64), "bad-digest"
        )

    class LaterDatetime(datetime):
        """模拟计划生成十六分钟后的真实时钟。"""

        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            """只影响计划有效期判断。"""
            return datetime.now(UTC) + timedelta(minutes=16)

    monkeypatch.setattr(plans_module, "datetime", LaterDatetime)
    with pytest.raises(FinanceError, match="PLAN_EXPIRED"):
        await execute(store, owner, plan)
    monkeypatch.undo()
    await store.release_invoice_allocation(
        owner, allocation["id"], allocation["version"], "release"
    )
    assert not (await store.get_action_plan(owner, plan["id"]))["executable"]
    light = policy(version=2).model_dump()
    light["record_mode"] = "internal_record"
    await store.configure_expenses([ExpensePolicy(**light)])
    current = await store.get_expense_claim(owner, doc["id"])
    new = await preview(store, owner, current)
    assert not new["executable"]
    assert current["record_mode"] == "invoice_backed"


async def test_migration_preserves_f0_records_and_pending_f1_workflow(store: FinanceStore) -> None:
    """幂等升级不清理F0收件和异常，也不重置F1待审批。"""
    owner, _ = await setup(store)
    ref = await receipt(store, owner)
    await invoice(store)
    doc = await submitted(store, owner)
    before = await store.get_expense_claim(owner, doc["id"])
    await store.migrate()
    assert await store.pool.fetchval("SELECT count(*) FROM finance_receipt WHERE id=$1", ref) == 1
    assert await store.get_expense_claim(owner, doc["id"]) == before
    assert await store.pool.fetchval("SELECT count(*) FROM finance_operation") == 1
    assert await store.pool.fetchval("SELECT count(*) FROM finance_action_plan") == 1
    assert await store.claim_import() is not None
    assert await store.pool.fetchval("SELECT count(*) FROM finance_business_operation") == 1
