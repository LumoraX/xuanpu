"""F2动作参数矩阵与精确金额的输入契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from xuanpu_finance.expenses.models import ActionPreview, AllocationRequest


def reference(kind: str = "supplier_account") -> dict[str, str]:
    """提供原契约要求的完整受控版本引用。"""
    return {"kind": kind, "source": "controlled", "id": "ref", "version": "v1", "sha256": "a" * 64}


@pytest.mark.parametrize("target", ["expense_claim", "payable"])
def test_payment_action_accepts_only_exact_amount_and_complete_account_reference(
    target: str,
) -> None:
    """付款可绑定两种业务单据，不能丢失账户版本摘要。"""
    body = ActionPreview(
        action="execute_payment",
        target_type=target,
        target_id="doc",
        expected_version=2,
        parameters={"amount": "25.50", "payee_ref": reference()},
    )
    assert body.parameters.model_dump(exclude_none=True) == {
        "amount": "25.50",
        "payee_ref": reference(),
    }


def test_payable_allocation_uses_shared_invoice_contract() -> None:
    """应付和报销共享分票金额与CAS参数。"""
    body = AllocationRequest(
        invoice_id="i",
        invoice_version=1,
        document_id="p",
        document_kind="payable",
        expected_document_version=1,
        amount="10.00",
        tax="1.00",
    )
    assert body.document_kind == "payable"


@pytest.mark.parametrize(
    "amount", ["0.00", "-1.00", "1e2", "01.00", "1.0", 1.0, "10000000000000000.00"]
)
def test_external_action_rejects_nonpositive_or_imprecise_amount(amount: object) -> None:
    """所有外部动作金额只接受有界正数两位十进制字符串。"""
    with pytest.raises(ValidationError):
        ActionPreview(
            action="execute_payment",
            target_type="payable",
            target_id="doc",
            expected_version=1,
            parameters={"amount": amount, "payee_ref": reference()},
        )


@pytest.mark.parametrize(
    "action,target,parameters",
    [
        ("execute_payment", "posting", {"amount": "1.00", "payee_ref": reference()}),
        ("submit_posting", "payable", {"amount": "1.00", "accounting_period": "2026-13"}),
        ("reverse_posting", "posting", {"amount": "1.00", "accounting_period": "2026-10"}),
        ("approve", "action_plan", {"workflow_step_id": "s", "amount": "1.00"}),
        ("submit_payable", "expense_claim", {}),
    ],
)
def test_f2_action_parameter_and_target_matrix_is_strict(
    action: str, target: str, parameters: dict[str, object]
) -> None:
    """拒绝不适用的参数、目标类型及无效期间。"""
    with pytest.raises(ValidationError):
        ActionPreview(
            action=action,
            target_type=target,
            target_id="d",
            expected_version=1,
            parameters=parameters,
        )
