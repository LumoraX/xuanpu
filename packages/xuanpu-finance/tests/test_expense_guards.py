"""补充计划、制度时限和异常响应的领域验收。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import ActionPreview, ExpensePolicy

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore

from .test_expense_domain import (
    allocate,
    claim,
    execute,
    invoice,
    policy,
    preview,
    setup,
    submitted,
)


async def test_passed_is_the_verified_invoice_contract_status(store: FinanceStore) -> None:
    """正式票据制度仅接受契约的passed查验事实。"""
    owner, _ = await setup(store)
    strict = policy(version=2).model_dump()
    strict["require_verified_invoice"] = True
    await store.configure_expenses([ExpensePolicy(**strict)])
    await invoice(store)
    doc = await claim(store, owner)
    await allocate(store, owner, doc)
    await store.pool.execute(
        "UPDATE finance_invoice SET data=jsonb_set(data,'{verification_status}','\"passed\"') WHERE id='i'"
    )
    plan = await preview(store, owner, await store.get_expense_claim(owner, doc["id"]))
    assert plan["executable"], plan["blockers"]
    await execute(store, owner, plan)


async def test_overdue_step_stays_pending_and_blocks_approval_but_allows_withdraw(
    store: FinanceStore,
) -> None:
    """超时不自动批准，待办保留供核对并允许申请人撤回。"""
    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    await store.pool.execute("UPDATE finance_workflow SET due_at=now()-interval '1 hour'")
    queue = await store.list_review_queue(people[0], "e")
    assert queue["total"] == 1
    step = queue["items"][0]
    plan = await preview(
        store, people[0], doc, "approve", workflow_step_id=step["workflow_step_id"]
    )
    assert not plan["executable"]
    with pytest.raises(FinanceError, match="WORKFLOW_OVERDUE"):
        await execute(store, people[0], plan)
    withdraw = await preview(store, owner, doc, "withdraw", reason="超时后重新核对")
    assert withdraw["executable"]
    await execute(store, owner, withdraw)


async def test_reenable_same_policy_cannot_resurrect_old_workflow_or_plan(
    store: FinanceStore,
) -> None:
    """撤销后恢复同版本制度不能复活旧授权，必须申请人重新提交。"""
    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    step = (await store.list_review_queue(people[0], "e"))["items"][0]
    plan = await preview(
        store, people[0], doc, "approve", workflow_step_id=step["workflow_step_id"]
    )
    await store.configure_expenses([])
    await store.configure_expenses([policy()])
    assert not (await store.get_action_plan(people[0], plan["id"]))["executable"]
    with pytest.raises(FinanceError):
        await execute(store, people[0], plan)
    fresh = await store.preview_finance_action(
        people[0],
        ActionPreview(
            action="approve",
            target_type="expense_claim",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"workflow_step_id": step["workflow_step_id"]},
        ),
        "new-preview",
    )
    assert not fresh["executable"]


async def test_regrant_approver_does_not_resurrect_old_assignment(store: FinanceStore) -> None:
    """人员撤权再恢复时旧指派必须撤回重提，相同配置重复启动保持稳定。"""
    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    step = (await store.list_review_queue(people[0], "e"))["items"][0]
    old = await preview(store, people[0], doc, "approve", workflow_step_id=step["workflow_step_id"])
    await store.grant("t", "a", "e", "approver")
    assert (await store.get_action_plan(people[0], old["id"]))["executable"]
    await store.grant("t", "a", "e", "collector")
    await store.grant("t", "a", "e", "approver")
    assert not (await store.get_action_plan(people[0], old["id"]))["executable"]
    fresh = await store.preview_finance_action(
        people[0],
        ActionPreview(
            action="approve",
            target_type="expense_claim",
            target_id=doc["id"],
            expected_version=doc["version"],
            parameters={"workflow_step_id": step["workflow_step_id"]},
        ),
        "regained",
    )
    assert not fresh["executable"]
    withdraw = await preview(store, owner, doc, "withdraw", reason="人员变更重新申请")
    await execute(store, owner, withdraw)


async def test_downgraded_approver_loses_assigned_record_and_invoice_visibility(
    store: FinanceStore,
) -> None:
    """撤销审批角色但保留普通公司权限，不继续泄露他人票据或单据。"""
    owner, people = await setup(store)
    await invoice(store)
    doc = await submitted(store, owner)
    assert (await store.get_expense_claim(people[0], doc["id"]))["id"] == doc["id"]
    await store.grant("t", "a", "e", "collector")
    with pytest.raises(FinanceError):
        await store.get_expense_claim(people[0], doc["id"])
    with pytest.raises(FinanceError):
        await store.get_invoice(people[0], "i")
    assert (await store.list_expense_claims(people[0], "e"))["total"] == 0
    assert (await store.list_invoices(people[0], "e"))["total"] == 0
    assert (await store.list_review_queue(people[0], "e"))["total"] == 0


@pytest.mark.parametrize("line", [None, "i-line"])
async def test_cumulative_tax_is_checked_even_when_gross_capacity_is_sufficient(
    store: FinanceStore, line: str | None
) -> None:
    """票头或指定行税额累计独立于可用金额做硬约束。"""
    owner, _ = await setup(store)
    await invoice(store, line=True)
    doc = await claim(store, owner, "400.00")
    await allocate(store, owner, doc, amount="200.00", tax="30.00" if line else "60.00", line=line)
    current = await store.get_expense_claim(owner, doc["id"])
    await allocate(
        store, owner, current, amount="200.00", tax="31.00" if line else "61.00", line=line
    )
    plan = await preview(store, owner, await store.get_expense_claim(owner, doc["id"]))
    assert not plan["executable"]
    with pytest.raises(
        FinanceError, match="INVOICE_LINE_LIMIT" if line else "INVOICE_BALANCE_EXCEEDED"
    ):
        await execute(store, owner, plan)
    assert (
        await store.pool.fetchval("SELECT count(*) FROM finance_allocation WHERE state='reserved'")
        == 0
    )


async def test_actual_f0_only_schema_upgrades_without_removing_receipts_or_exceptions(
    store: FinanceStore,
) -> None:
    """在同一独立测试库的临时schema真正先建F0再应用F1迁移。"""
    from uuid import uuid4

    from xuanpu_finance.storage.f1_schema import F1_SCHEMA
    from xuanpu_finance.storage.schema import SCHEMA

    schema = "f0_upgrade_" + uuid4().hex
    async with store.pool.acquire() as conn, conn.transaction():
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET LOCAL search_path TO "{schema}"')
        await conn.execute(SCHEMA)
        await conn.execute(
            "INSERT INTO finance_company(tenant_id,legal_entity_id,name,tax_id) VALUES('old','old','旧公司','OLD')"
        )
        await conn.execute("INSERT INTO finance_grant VALUES('old','old','old','collector')")
        await conn.execute(
            "INSERT INTO finance_operation(tenant_id,legal_entity_id,id,owner_id,idempotency_key,request_hash,trace_id) VALUES('old','old','old','old','old','old','old')"
        )
        await conn.execute(
            "INSERT INTO finance_item(tenant_id,legal_entity_id,operation_id,position,file_ref) VALUES('old','old','old',0,'{\"file_id\":\"f\",\"revision\":1}')"
        )
        await conn.execute(
            "INSERT INTO finance_receipt VALUES('old','old','receipt','old','old',0,'obj','digest','{\"file_id\":\"f\",\"revision\":1}')"
        )
        await conn.execute(
            "INSERT INTO finance_exception(tenant_id,legal_entity_id,id,owner_id,operation_id,item_position,rule_id,receipt_id) VALUES('old','old','exception','old','old',0,'ORIGINAL_REQUIRED','receipt')"
        )
        await conn.execute(F1_SCHEMA)
        await conn.execute(F1_SCHEMA)
        assert await conn.fetchval("SELECT object_key FROM finance_receipt") == "obj"
        assert await conn.fetchval("SELECT rule_id FROM finance_exception") == "ORIGINAL_REQUIRED"
        assert await conn.fetchval("SELECT generation FROM finance_grant_epoch") == 1
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
