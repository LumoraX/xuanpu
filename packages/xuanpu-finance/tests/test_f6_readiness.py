"""F6在线检查只读当前持久配置且不授予动作权限。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError

from .test_f5_domain import setup_funding


async def test_current_roles_company_isolation_digest_and_no_writes(store: Any) -> None:
    """撤权、账户当前绑定及启停改变摘要；检查本身不写业务状态。"""
    owner, payer, _, approver = await setup_funding(store)
    assert hasattr(store, "get_finance_readiness"), "缺少F6只读readiness接口"
    before = await store.pool.fetchval(
        "SELECT sum(n_tup_ins+n_tup_upd+n_tup_del) FROM pg_stat_user_tables WHERE schemaname=current_schema()"
    )
    first = await store.get_finance_readiness(payer, "e")
    assert first["production_status"] == "not_verified"
    assert first["status"] == "blocked"
    raw = json.dumps(first)
    for secret in ("provider-account", "127.0.0.1", "account_name", "approver_user_id", "BUYER"):
        assert secret not in raw
    assert before == await store.pool.fetchval(
        "SELECT sum(n_tup_ins+n_tup_upd+n_tup_del) FROM pg_stat_user_tables WHERE schemaname=current_schema()"
    )
    for who, entity in (
        (owner, "e"),
        (approver, "e"),
        (replace(payer, tenant_id="other"), "e"),
        (payer, "other"),
    ):
        with pytest.raises(FinanceError) as error:
            await store.get_finance_readiness(who, entity)
        assert error.value.status == 403
    await store.grant("t", "a", "e", "collector")
    second = await store.get_finance_readiness(payer, "e")
    assert first["configuration_digest"] != second["configuration_digest"]
    assert next(c for c in second["checks"] if c["code"] == "approval_roles")["status"] == "blocked"
    await store.pool.execute(
        "UPDATE finance_settlement_catalog SET enabled=false,generation=generation+1 WHERE tenant_id='t' AND legal_entity_id='e' AND kind='supplier_account'"
    )
    third = await store.get_finance_readiness(payer, "e")
    assert second["configuration_digest"] != third["configuration_digest"]
    await store.pool.execute(
        "DELETE FROM finance_grant WHERE tenant_id='t' AND legal_entity_id='e' AND user_id='payer'"
    )
    with pytest.raises(FinanceError):
        await store.get_finance_readiness(payer, "e")


async def fully_configured(store: Any) -> Any:
    """四类制度及五场景通道都配置的隔离实例。"""
    from xuanpu_finance.storage.common import decode

    from .test_f3_domain import configured as compliance_configured
    from .test_f5_domain import SCENARIOS, funding_catalog

    _, payer, _, _ = await setup_funding(store)
    await compliance_configured(store)
    provider = decode(await store.pool.fetchval("SELECT data FROM finance_settlement_provider"))
    provider.update(
        configuration_version=2, enabled=True, supported_funding_scenarios=list(SCENARIOS)
    )
    await funding_catalog(store, provider=provider)
    return payer


async def test_fully_configured_simulation_and_each_live_toggle(store: Any) -> None:
    """完整模拟配置可用，当前制度或通道停用立即阻断并改变摘要。"""
    payer = await fully_configured(store)
    first = await store.get_finance_readiness(payer, "e")
    assert first["status"] == "configured_simulation", first["checks"]
    assert first["production_status"] == "not_verified"
    assert len(first["versions"]) == 4
    for table in (
        "finance_expense_policy",
        "finance_settlement_policy",
        "finance_compliance_policy",
        "finance_funding_policy",
    ):
        await store.pool.execute(
            f"UPDATE {table} SET enabled=false,generation=generation+1 WHERE tenant_id='t' AND legal_entity_id='e'"
        )
        disabled = await store.get_finance_readiness(payer, "e")
        assert disabled["status"] == "blocked"
        assert disabled["configuration_digest"] != first["configuration_digest"]
        await store.pool.execute(
            f"UPDATE {table} SET enabled=true,generation=generation+1 WHERE tenant_id='t' AND legal_entity_id='e'"
        )
    for table in ("finance_settlement_provider", "finance_compliance_provider"):
        await store.pool.execute(
            f"UPDATE {table} SET enabled=false,generation=generation+1 WHERE tenant_id='t' AND legal_entity_id='e'"
        )
        disabled = await store.get_finance_readiness(payer, "e")
        assert disabled["status"] == "blocked"
        await store.pool.execute(
            f"UPDATE {table} SET enabled=true,generation=generation+1 WHERE tenant_id='t' AND legal_entity_id='e'"
        )


async def test_payment_only_needs_three_payable_funding_scenarios(store: Any) -> None:
    """核销和退回只记账，合法付款通道只需声明三个可付款场景。"""
    from xuanpu_finance.readiness.checks import build_report

    payer = await fully_configured(store)
    async with store.pool.acquire() as conn:
        snapshot = await store._readiness_snapshot(conn, payer.tenant_id, "e")
    posting = next(p for p in snapshot["providers"] if p["provider"] == "test" and p["enabled"])
    payment = {
        **posting,
        "provider": "payment-only",
        "configuration_version": 1,
        "supported_funding_scenarios": ["prepayment", "employee_advance", "entrusted_collection"],
    }
    snapshot["providers"].append(payment)
    snapshot["policies"]["settlement"].update(
        payment_provider="payment-only", payment_configuration_version=1
    )
    assert build_report(snapshot)["status"] == "configured_simulation"
    payment["supported_funding_scenarios"].remove("employee_advance")
    assert build_report(snapshot)["status"] == "blocked"


async def test_internal_record_policy_cannot_claim_funding_ready(store: Any) -> None:
    """内部台账制度无法支撑资金执行，即使其他四类配置都完整。"""
    from xuanpu_finance.expenses.models import ExpensePolicy
    from xuanpu_finance.storage.common import decode

    payer = await fully_configured(store)
    first = await store.get_finance_readiness(payer, "e")
    assert first["status"] == "configured_simulation"
    raw = decode(
        await store.pool.fetchval(
            "SELECT r.data FROM finance_expense_policy p JOIN finance_expense_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id='t' AND p.legal_entity_id='e'"
        )
    )
    raw.update(
        version=raw["version"] + 1, record_mode="internal_record", require_verified_invoice=False
    )
    await store.configure_expenses([ExpensePolicy.model_validate(raw)])
    report = await store.get_finance_readiness(payer, "e")
    assert report["status"] == "blocked"
    assert report["configuration_digest"] != first["configuration_digest"]
    assert next(c for c in report["checks"] if c["code"] == "invoice_basis")["status"] == "blocked"


@pytest.mark.parametrize(
    "changes",
    [
        {"archive_profile_id": "unverified-profile"},
        {"archive_profile_version": 999},
        {"required_lock_mode": "COMPLIANCE"},
    ],
)
async def test_archive_profile_matches_domain_eligibility(
    store: Any, changes: dict[str, Any]
) -> None:
    """准备检查不得把领域明确拒绝的档案profile标为模拟可用。"""
    from xuanpu_finance.compliance.models import CompliancePolicy
    from xuanpu_finance.storage.common import decode

    payer = await fully_configured(store)
    first = await store.get_finance_readiness(payer, "e")
    assert first["status"] == "configured_simulation"
    raw = decode(
        await store.pool.fetchval(
            "SELECT r.data FROM finance_compliance_policy p JOIN finance_compliance_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id='t' AND p.legal_entity_id='e'"
        )
    )
    raw.update(version=raw["version"] + 1, enabled=True, **changes)
    providers = [
        {**decode(row["data"]), "enabled": row["enabled"]}
        for row in await store.pool.fetch(
            "SELECT data,enabled FROM finance_compliance_provider WHERE tenant_id='t' AND legal_entity_id='e'"
        )
    ]
    await store.configure_compliance(
        [CompliancePolicy.model_validate(raw)], providers, simulation=True
    )
    report = await store.get_finance_readiness(payer, "e")
    assert report["status"] == "blocked"
    assert report["configuration_digest"] != first["configuration_digest"]
    assert (
        next(c for c in report["checks"] if c["code"] == "archive_capabilities")["status"]
        == "blocked"
    )
