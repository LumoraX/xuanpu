"""报销制度和审批人员必须显式配置，F0配置保持兼容。"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from xuanpu_finance.config import FinanceConfig

from .test_config import settings


def configured() -> dict[str, Any]:
    """明确测试公司、经办人和两个不同审批人，不暗示真实人员配置。"""
    return settings(
        companies=[
            {
                "tenant_id": "acme",
                "legal_entity_id": "company-a",
                "name": "测试公司",
                "tax_id": "TEST",
            }
        ],
        grants=[
            {
                "tenant_id": "acme",
                "legal_entity_id": "company-a",
                "user_id": "employee",
                "role": "collector",
            },
            {
                "tenant_id": "acme",
                "legal_entity_id": "company-a",
                "user_id": "manager",
                "role": "approver",
            },
            {
                "tenant_id": "acme",
                "legal_entity_id": "company-a",
                "user_id": "finance",
                "role": "reviewer",
            },
        ],
        expense_policies=[
            {
                "tenant_id": "acme",
                "legal_entity_id": "company-a",
                "version": 1,
                "enabled": True,
                "categories": [{"id": "travel", "name": "差旅"}],
                "cost_centers": [],
                "steps": [
                    {"id": "manager", "name": "主管审批", "approver_user_id": "manager"},
                    {"id": "finance", "name": "财务审核", "approver_user_id": "finance"},
                ],
                "exception_owner_user_id": "finance",
            }
        ],
    )


def test_f0_config_has_no_implicit_reimbursement_policy() -> None:
    """没有制度时只运行F0，不自动授予任何人审批权限。"""
    config = FinanceConfig.model_validate(settings())
    assert getattr(config, "expense_policies", None) == []


def test_explicit_policy_and_assigned_approver_role_are_supported() -> None:
    """人工审批人来自配置，不能用平台管理员代替。"""
    config = FinanceConfig.model_validate(configured())
    assert config.expense_policies[0].steps[0].approver_user_id == "manager"


@pytest.mark.parametrize(
    "change", ["foreign_company", "missing_approver", "wrong_exception_owner", "duplicate_policy"]
)
def test_policy_configuration_rejects_invalid_links(change: str) -> None:
    """在启动前拒绝失联制度/人员，避免无人审批或跨公司资格。"""
    data = configured()
    policy = data["expense_policies"][0]
    if change == "foreign_company":
        policy["legal_entity_id"] = "foreign"
    elif change == "missing_approver":
        data["grants"] = [entry for entry in data["grants"] if entry["user_id"] != "manager"]
    elif change == "wrong_exception_owner":
        policy["exception_owner_user_id"] = "employee"
    else:
        data["expense_policies"].append(policy.copy())
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(data)
