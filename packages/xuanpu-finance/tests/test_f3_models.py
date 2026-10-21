"""F3严格税务、审批与策略契约。"""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError


def models():
    """延迟导入以明确报告尚未实现的新契约。"""
    return importlib.import_module("xuanpu_finance.compliance.models")


def test_policy_defaults_closed_and_bounds_plan_and_verification() -> None:
    """新策略缺省关闭，查验和计划有效期有上限。"""
    policy = models().CompliancePolicy(tenant_id="t", legal_entity_id="c", version=1)
    assert policy.enabled is False
    assert policy.tax_enabled is False
    assert policy.archive_enabled is False
    for field, value in [("plan_hours", 73), ("verification_max_age_hours", 169)]:
        with pytest.raises(ValidationError):
            models().CompliancePolicy(tenant_id="t", legal_entity_id="c", version=1, **{field: value})


def test_tax_snapshot_requires_authority_revision_and_aware_times() -> None:
    """快照不接受聊天式无来源结论及无时区时间。"""
    snapshot = models().TaxSnapshot.model_validate(tax_snapshot())
    assert snapshot.authority_revision == 1
    for changed in [{"authority_revision": 0}, {"observed_at": "2026-10-21T00:00:00"}, {"unexpected": True}]:
        with pytest.raises(ValidationError):
            models().TaxSnapshot.model_validate({**tax_snapshot(), **changed})


def tax_snapshot() -> dict[str, object]:
    """给出真实外部快照必须具备的完整结构。"""
    return {"kind": "tax", "authority_revision": 1, "observed_at": "2026-10-21T00:00:00+00:00", "valid_until": "2026-10-22T00:00:00+00:00", "verification_status": "passed", "verification_id": "verification", "invoice_status": "normal", "usage_status": "unconfirmed", "tax_entry_status": "unconfirmed", "red_letter_cases": [], "adjustments": []}


def test_action_parameters_cannot_supply_derived_amount_or_create_red_case() -> None:
    """确认动作只接受实际期间或已有确认单，金额只能服务端推导。"""
    model = models().ComplianceActionPreview
    valid = dict(action="confirm_usage", target_type="invoice", target_id="i", expected_version=1)
    assert model(**valid, parameters={"accounting_period": "2026-10"}).action == "confirm_usage"
    for parameters in [{"accounting_period": "2026-13"}, {"accounting_period": "2026-10", "amount": "1.00"}, {}]:
        with pytest.raises(ValidationError):
            model(**valid, parameters=parameters)
    with pytest.raises(ValidationError):
        model(action="create_red_letter", target_type="invoice", target_id="i", expected_version=1, parameters={})


def test_provider_descriptor_rejects_credentials_and_incomplete_archive_capabilities() -> None:
    """持久描述不允许凭据路径，启用归档须具备版本与保留读回能力。"""
    descriptor = dict(tenant_id="t",legal_entity_id="e",provider="archive",configuration_version=1,adapter_version="v1",base_url="http://127.0.0.1:1",simulation=True,enabled=True,kind="archive",supports_idempotency=True,supports_query=True,supports_object_versions=True,supports_retention_readback=True)
    model=models().ComplianceProviderDescriptor
    assert model(**descriptor).kind == "archive"
    for changes in [{"token_file":"secret"},{"supports_object_versions":False},{"supports_retention_readback":False}]:
        with pytest.raises(ValidationError):
            model(**{**descriptor,**changes})


def test_external_snapshot_rejects_database_overflow_and_null_text_before_materialization() -> None:
    """外部不合法值必须在DTO层拒绝，避免数据库错误回滚已保全原文。"""
    model=models().TaxSnapshot
    for changes in [{"authority_revision":9223372036854775808},{"verification_id":"bad\x00id"},{"adjustments":[{"external_adjustment_id":"a","status":"required","reason":"bad\x00text","amount":"1.00","tax":"0.00"}]}]:
        with pytest.raises(ValidationError):
            model.model_validate({**tax_snapshot(),**changes})
