"""纯函数校验当前公司快照，原始配置只参与计算不进入报告。"""

from __future__ import annotations

from typing import Any

from xuanpu_finance.compliance.archive_checks import archive_profile_eligible
from xuanpu_finance.funding.models import SCENARIOS
from xuanpu_finance.operations import request_digest
from xuanpu_finance.readiness.models import (
    EXTERNAL_VERIFICATIONS,
    ReadinessCheck,
    ReadinessReport,
    check,
)


def active_policy(snapshot: dict[str, Any], kind: str) -> dict[str, Any]:
    """缺少或停用的当前制度明确按阻断处理。"""
    value: dict[str, Any] = snapshot["policies"].get(kind, {})
    return value if value.get("enabled") else {}


def approvals_valid(snapshot: dict[str, Any]) -> bool:
    """所有已配置审批及异常岗位都必须仍有当前公司资格。"""
    roles = {entry["user_id"]: entry["role"] for entry in snapshot["roles"]}
    for kind in ("expense", "settlement", "compliance", "funding"):
        policy = active_policy(snapshot, kind)
        chains = [v for k, v in policy.items() if k.endswith("_steps")]
        if kind == "expense":
            chains += [policy.get("steps", [])]
            if roles.get(policy.get("exception_owner_user_id")) != "reviewer":
                return False
        if kind == "funding":
            chains += list(policy.get("steps", {}).values())
        for chain in chains:
            if not chain or any(
                roles.get(s["approver_user_id"]) not in {"reviewer", "approver"} for s in chain
            ):
                return False
    return True


def executors_valid(snapshot: dict[str, Any]) -> bool:
    """经办岗位必须仍是reviewer，付款与记账岗位严格分离。"""
    roles = {entry["user_id"]: entry["role"] for entry in snapshot["roles"]}
    settlement = active_policy(snapshot, "settlement")
    pay = settlement.get("payment_executor_user_ids", [])
    post = settlement.get("posting_executor_user_ids", [])
    if not pay or not post or set(pay) & set(post):
        return False
    groups = [pay, post]
    compliance = active_policy(snapshot, "compliance")
    groups += [compliance.get(k + "_executor_user_ids", []) for k in ("tax", "archive")]
    return all(group and all(roles.get(u) == "reviewer" for u in group) for group in groups)


def bound_provider(snapshot: dict[str, Any], policy: dict[str, Any], kind: str) -> dict[str, Any]:
    """精确匹配公司快照中的当前启用渠道及冻结版本。"""
    for provider in snapshot["providers"]:
        if (
            provider.get("provider") == policy.get(kind + "_provider")
            and provider.get("configuration_version") == policy.get(kind + "_configuration_version")
            and provider.get("enabled")
            and provider.get("simulation") == snapshot["simulation"]
        ):
            if kind in {"tax", "archive"} and provider.get("kind") != kind:
                continue
            return dict(provider)
    return {}


def provider_valid(provider: dict[str, Any], kind: str) -> bool:
    """结算与合规通道需要可安全恢复及相应事实读回能力。"""
    required = ["supports_idempotency", "supports_query"]
    if kind == "tax":
        required += ["supports_state_revision"]
    if kind == "archive":
        required += ["supports_object_versions", "supports_retention_readback"]
    return bool(provider) and all(provider.get(k) is True for k in required)


def capability_checks(snapshot: dict[str, Any]) -> list[ReadinessCheck]:
    """逐项检查制度、审批、账簿账户、五场景及合规前提。"""
    expense = active_policy(snapshot, "expense")
    settlement = active_policy(snapshot, "settlement")
    compliance = active_policy(snapshot, "compliance")
    funding = active_policy(snapshot, "funding")
    providers = {
        k: bound_provider(snapshot, settlement if k in {"payment", "posting"} else compliance, k)
        for k in ("payment", "posting", "tax", "archive")
    }
    accounts = [
        a
        for a in snapshot["catalog"]
        if a.get("enabled")
        and a.get("verified") is True
        and a.get("provider_account_id")
        and a.get("allowed_user_ids")
    ]
    return [
        check("company", bool(snapshot["company"]), "公司基础资料须完整配置"),
        check(
            "policies", all((expense, settlement, compliance, funding)), "四类当前制度须显式启用"
        ),
        check("approval_roles", approvals_valid(snapshot), "各审批及异常岗位须有当前公司资格"),
        check("executor_roles", executors_valid(snapshot), "经办岗位须有效且付款记账分离"),
        check(
            "settlement_channels",
            all(provider_valid(providers[k], k) for k in ("payment", "posting")),
            "结算绑定版本须具备幂等及查单能力",
        ),
        check(
            "ledger_periods",
            bool(settlement.get("ledger_id") and settlement.get("open_periods")),
            "须配置账簿和开放期间",
        ),
        check("controlled_accounts", bool(accounts), "须存在当前启用且已核实的受控账户"),
        check(
            "funding_chains",
            all(funding.get("steps", {}).get(s) for s in SCENARIOS),
            "资金五场景须分别有实际审批链",
        ),
        check(
            "funding_capabilities",
            all(
                set(required) <= set(providers[k].get("supported_funding_scenarios", []))
                for k, required in (("payment", SCENARIOS[:3]), ("posting", SCENARIOS))
            ),
            "资金场景须被绑定结算通道显式支持",
        ),
        check(
            "invoice_basis",
            expense.get("record_mode") == "invoice_backed",
            "专用资金须基于有效发票制度，内部台账不能提升执行资格",
        ),
        *compliance_checks(compliance, providers, snapshot["simulation"]),
    ]


def compliance_checks(
    compliance: dict[str, Any], providers: dict[str, dict[str, Any]], simulation: bool
) -> list[ReadinessCheck]:
    """税务票样与档案标准必须由精确绑定渠道提供相应能力。"""
    return [
        check(
            "tax_capabilities",
            bool(
                compliance.get("tax_enabled")
                and compliance.get("open_periods")
                and compliance.get("supported_invoice_profiles")
                and provider_valid(providers["tax"], "tax")
            ),
            "税务须具备期间票样及权威版本能力",
        ),
        check(
            "archive_capabilities",
            bool(
                compliance.get("archive_enabled")
                and archive_profile_eligible(
                    simulation,
                    compliance.get("archive_profile_id"),
                    compliance.get("archive_profile_version"),
                    compliance.get("required_lock_mode"),
                )
                and provider_valid(providers["archive"], "archive")
            ),
            "归档须具备标准配置及版本留存读回能力",
        ),
    ]


def build_report(
    snapshot: dict[str, Any], extra: list[ReadinessCheck] | None = None
) -> ReadinessReport:
    """只输出白名单摘要，正式模式始终保留外部未验边界。"""
    checks = [*(extra or []), *capability_checks(snapshot)]
    versions = [
        {
            "kind": kind,
            **{k: data[k] for k in ("version", "generation", "enabled", "digest") if k in data},
        }
        for kind, data in sorted(snapshot["policies"].items())
    ]
    status: Any = (
        "blocked"
        if any(c["status"] == "blocked" for c in checks)
        else "configured_simulation"
        if snapshot["simulation"]
        else "not_verified"
    )
    return {
        "status": status,
        "production_status": "not_verified",
        "checks": checks,
        "versions": versions,
        "configuration_digest": request_digest(snapshot),
        "external_verifications": EXTERNAL_VERIFICATIONS.copy(),
    }
