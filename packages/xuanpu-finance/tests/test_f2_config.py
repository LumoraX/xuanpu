"""F2执行环境必须显式隔离，未配置通道不默认获得付款能力。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from xuanpu_finance.config import FinanceConfig

from .test_config import settings


def test_f1_config_keeps_external_execution_closed() -> None:
    """旧配置兼容且没有隐式付款、记账或模拟通道。"""
    config = FinanceConfig.model_validate(settings())
    assert config.execution_simulation is False
    assert config.settlement_policies == []
    assert config.settlement_providers == []
    assert config.business_documents == [] and config.payee_accounts == []


def test_production_rejects_execution_simulation() -> None:
    """内部测试模式不能被用于正式资金或正式凭证。"""
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(settings(execution_simulation=True))


def test_test_environment_can_explicitly_select_simulation() -> None:
    """只是结构许可；实际数据库和单据环境还由运行时/领域核验。"""
    config = FinanceConfig.model_validate(settings(environment="test", execution_simulation=True))
    assert config.execution_simulation


def settlement_settings() -> dict:
    """独立模拟的最小完整人员/固定渠道配置，不包含实际密钥。"""
    scope = {"tenant_id": "t", "legal_entity_id": "e"}
    step = {"id": "review", "name": "专用审批", "approver_user_id": "approver"}
    return settings(
        environment="test",
        execution_simulation=True,
        companies=[{**scope, "name": "测试公司", "tax_id": "TEST"}],
        grants=[
            {**scope, "user_id": user, "role": role}
            for user, role in [
                ("payer", "reviewer"),
                ("accountant", "reviewer"),
                ("approver", "approver"),
            ]
        ],
        settlement_providers=[
            {
                **scope,
                "provider": "isolated",
                "configuration_version": 1,
                "adapter_version": "http-v1",
                "base_url": "http://127.0.0.1:9999",
                "token_file": "token",
                "callback_secret_file": "callback",
                "callback_key_id": "k1",
                "simulation": True,
                "supports_idempotency": True,
                "supports_query": True,
            }
        ],
        settlement_policies=[
            {
                **scope,
                "version": 1,
                "enabled": True,
                "payment_provider": "isolated",
                "payment_configuration_version": 1,
                "posting_provider": "isolated",
                "posting_configuration_version": 1,
                "ledger_id": "test-ledger",
                "open_periods": ["2026-10"],
                "payment_executor_user_ids": ["payer"],
                "posting_executor_user_ids": ["accountant"],
                "payment_steps": [step],
                "posting_steps": [step],
                "reversal_steps": [step],
                "require_verified_invoice": False,
            }
        ],
    )


def test_complete_simulation_config_accepts_fixed_scopes_and_paths() -> None:
    """显式完整配置可运行，但没有取得正式执行资格。"""
    config = FinanceConfig.model_validate(settlement_settings())
    assert config.settlement_providers[0].token_file.name == "token"
    assert config.settlement_policies[0].enabled and config.execution_simulation


@pytest.mark.parametrize(
    "mutation",
    [
        "no_query",
        "no_idempotency",
        "wrong_company",
        "no_role",
        "overlap",
        "wrong_mode",
        "duplicate_selector",
        "no_approver",
    ],
)
def test_incomplete_or_ambiguous_authorization_is_rejected(mutation: str) -> None:
    """API启动前拒绝不可靠渠道、公司越权及不成立的实际岗位。"""
    raw = settlement_settings()
    if mutation == "no_query":
        raw["settlement_providers"][0]["supports_query"] = False
    if mutation == "no_idempotency":
        raw["settlement_providers"][0]["supports_idempotency"] = False
    if mutation == "wrong_company":
        raw["settlement_providers"][0]["legal_entity_id"] = "other"
    if mutation == "no_role":
        raw["grants"][0]["role"] = "collector"
    if mutation == "overlap":
        raw["settlement_policies"][0]["posting_executor_user_ids"] = ["payer"]
    if mutation == "wrong_mode":
        raw["settlement_providers"][0]["simulation"] = False
    if mutation == "duplicate_selector":
        raw["settlement_providers"].append(raw["settlement_providers"][0].copy())
    if mutation == "no_approver":
        raw["grants"].pop()
    with pytest.raises(ValidationError):
        FinanceConfig.model_validate(raw)
