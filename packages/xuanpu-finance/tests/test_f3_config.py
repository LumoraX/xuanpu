"""合规与归档默认关闭，税务配置必须指向真实公司岗位。"""
from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from xuanpu_finance.config import FinanceConfig

from .test_config import settings


def compliance_settings() -> dict[str, Any]:
    """仅配置隔离查验渠道和实际经办、审批两人。"""
    scope = dict(tenant_id="t", legal_entity_id="e")
    return settings(environment="test", execution_simulation=True,
        companies=[dict(**scope,name="测试",tax_id="TEST")],
        grants=[dict(**scope,user_id="taxer",role="reviewer"),
                dict(**scope,user_id="approver",role="approver")],
        compliance_providers=[dict(**scope,provider="tax",kind="tax",configuration_version=1,
            adapter_version="v1",base_url="http://127.0.0.1:9999",token_file="token",
            callback_secret_file="secret",callback_key_id="k",simulation=True,
            supports_idempotency=True,supports_query=True,supports_state_revision=True)],
        compliance_policies=[dict(**scope,version=1,enabled=True,tax_enabled=True,
            tax_provider="tax",tax_configuration_version=1,tax_executor_user_ids=["taxer"],
            tax_steps=[dict(id="tax-approve",name="税务审批",approver_user_id="approver")])])


def test_old_config_has_no_compliance_capability() -> None:
    """已有配置兼容但不取得新增税务或归档权限。"""
    config = FinanceConfig.model_validate(settings())
    assert config.compliance_policies == [] and config.compliance_providers == []


def test_explicit_simulation_compliance_config() -> None:
    """结构配置通过仍须运行时核验真实finance_test库。"""
    config = FinanceConfig.model_validate(compliance_settings())
    assert config.compliance_policies[0].tax_enabled


@pytest.mark.parametrize("change", ["role", "scope", "simulation", "selector", "approver"])
def test_bad_compliance_config_is_rejected(change: str) -> None:
    """禁止隐式岗位、跨公司、环境混用或歧义版本。"""
    raw = compliance_settings()
    if change == "role":
        raw["grants"][0]["role"] = "collector"
    elif change == "scope":
        raw["compliance_providers"][0]["legal_entity_id"] = "other"
    elif change == "simulation":
        raw["compliance_providers"][0]["simulation"] = False
    elif change == "selector":
        raw["compliance_providers"].append(dict(raw["compliance_providers"][0]))
    else:
        raw["compliance_policies"][0]["tax_steps"][0]["approver_user_id"] = "missing"
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(raw)
