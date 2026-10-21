"""报销写模型的严格格式和禁止自报身份边界。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_expense_models_exist_and_reject_noncanonical_money() -> None:
    """金额必须是规范字符串，费用非零且总额不可溢出。"""
    from xuanpu_finance.expenses.models import ExpenseDraft

    line = {
        "category_id": "travel",
        "amount": "1.00",
        "occurred_on": "2026-10-21",
        "reason": "出差",
    }
    for amount in (
        1.0,
        1,
        "1",
        "1.0",
        "01.00",
        "1e2",
        "NaN",
        "-1.00",
        "0.00",
        "10000000000000000.00",
    ):
        with pytest.raises(ValidationError):
            ExpenseDraft(legal_entity_id="a", reason="交通", lines=[{**line, "amount": amount}])
    with pytest.raises(ValidationError):
        ExpenseDraft(
            legal_entity_id="a",
            reason="交通",
            lines=[{**line, "amount": "9999999999999999.99"}, line],
        )
    for day in ("20261021", "2026-1-01", "2026-02-30", "2026-10-21T00:00:00"):
        with pytest.raises(ValidationError):
            ExpenseDraft(legal_entity_id="a", reason="交通", lines=[{**line, "occurred_on": day}])
    assert ExpenseDraft(legal_entity_id="a", reason="交通", lines=[line]).lines[0].amount == "1.00"


def test_action_parameter_matrix_and_allocation_tax() -> None:
    """F1仅允许四个动作，参数不允许自报用户或成功状态。"""
    from xuanpu_finance.expenses.models import ActionPreview, AllocationRequest

    base = {"target_type": "expense_claim", "target_id": "c", "expected_version": 1}
    for action, parameters in (
        ("submit_claim", {"reason": "x"}),
        ("reject", {"workflow_step_id": "s"}),
        ("withdraw", {}),
        ("approve", {"workflow_step_id": "s", "owner_id": "x"}),
        ("execute_payment", {}),
    ):
        with pytest.raises(ValidationError):
            ActionPreview(**base, action=action, parameters=parameters)
    ActionPreview(**base, action="approve", parameters={"workflow_step_id": "s"})
    with pytest.raises(ValidationError):
        AllocationRequest(
            invoice_id="i",
            invoice_version=1,
            document_id="c",
            document_kind="expense_claim",
            expected_document_version=1,
            amount="1.00",
            tax="1.01",
        )


def test_internal_policy_must_explicitly_disable_invoice_verification() -> None:
    """内部模式不能同时要求无票无法满足的官方发票查验。"""
    from xuanpu_finance.expenses.models import ExpensePolicy

    with pytest.raises(ValidationError):
        ExpensePolicy(
            tenant_id="t",
            legal_entity_id="e",
            version=1,
            enabled=True,
            categories=[],
            cost_centers=[],
            steps=[{"id": "a", "name": "审批", "approver_user_id": "a"}],
            exception_owner_user_id="r",
            record_mode="internal_record",
        )
