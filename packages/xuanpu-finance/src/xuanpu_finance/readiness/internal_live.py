"""内部记录只读核对：固定检查和摘要不泄露原始配置，也不授予执行许可。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from xuanpu_finance.expenses.models import ExpensePolicy
from xuanpu_finance.operations import request_digest
from xuanpu_finance.readiness.models import ReadinessCheck, check

EXTERNAL_VERIFICATIONS = [
    "formal_policy_approval",
    "production_identity",
    "database_and_original_isolation",
    "offsite_disaster_recovery",
    "real_model",
    "production_load",
]
CHECK_DETAILS = {
    "company": "公司基础资料须完整配置",
    "expense_policy": "须启用有效内部记录制度并关闭发票查验要求",
    "expense_categories": "启用制度须至少包含一个有效费用分类",
    "approval_roles": "审批及异常责任岗位须有当前公司资格",
    "collector_separation": "须存在审批链之外的当前收件岗位",
    "grant_epochs": "现行授权须匹配同角色的正授权代际",
    "external_configuration": "本公司不得启用外部制度、渠道或目录",
    "simulation_disabled": "结算及合规模拟须显式关闭",
    "runtime_mode": "当前模式须允许内部核对",
    "runtime_instance_lock": "实例独占锁须保持有效",
    "runtime_workers": "原件收件和站内通知任务须实际运行",
    "runtime_external_workers": "内部实例不得装配外部执行器或渠道注册表",
}


def internal_policy(snapshot: dict[str, Any]) -> ExpensePolicy | None:
    """使用领域Pydantic模型验证持久制度，只将明确的模型错误转为阻断。"""
    raw = snapshot["policies"].get("expense")
    if raw is None:
        return None
    try:
        policy = ExpensePolicy.model_validate(
            {k: v for k, v in raw.items() if k not in {"generation", "digest"}}
        )
        if (policy.tenant_id, policy.legal_entity_id) != (
            snapshot["scope"]["tenant_id"],
            snapshot["scope"]["legal_entity_id"],
        ):
            return None
        return policy
    except ValidationError:
        return None


def configuration_checks(snapshot: dict[str, Any]) -> list[ReadinessCheck]:
    """只检查当前公司授权；撤销后留下的epoch墓碑不要求仍有grant。"""
    policy = internal_policy(snapshot)
    roles = {r["user_id"]: r["role"] for r in snapshot["roles"]}
    valid_policy = bool(
        policy
        and policy.enabled
        and policy.record_mode == "internal_record"
        and policy.require_verified_invoice is False
    )
    approvers = {step.approver_user_id for step in policy.steps} if policy else set()
    approvals = bool(
        policy
        and policy.steps
        and roles.get(policy.exception_owner_user_id) == "reviewer"
        and all(roles.get(user) in {"reviewer", "approver"} for user in approvers)
    )
    collectors = {user for user, role in roles.items() if role == "collector"}
    epoch_valid = all(
        r["role"] == r["epoch_role"]
        and isinstance(r["generation"], int)
        and not isinstance(r["generation"], bool)
        and r["generation"] > 0
        for r in snapshot["roles"]
    )
    outcomes = {
        "company": all(
            isinstance(snapshot["company"].get(k), str) and snapshot["company"][k].strip()
            for k in ("name", "tax_id")
        ),
        "expense_policy": valid_policy,
        "expense_categories": bool(valid_policy and policy and policy.categories),
        "approval_roles": approvals,
        "collector_separation": bool(policy and collectors - approvers),
        "grant_epochs": epoch_valid,
        "external_configuration": not any(
            r["enabled"] for rows in snapshot["external_current"].values() for r in rows
        ),
        "simulation_disabled": snapshot["settlement_simulation"] is False
        and snapshot["compliance_simulation"] is False,
    }
    return [check(code, bool(passed), CHECK_DETAILS[code]) for code, passed in outcomes.items()]


def build_internal_report(snapshot: dict[str, Any]) -> dict[str, Any]:
    """仅白名单字段出事务；配置与授权代际摘要不含运行时间或业务数据。"""
    checks = configuration_checks(snapshot)
    expense = snapshot["policies"].get("expense")
    versions = (
        []
        if expense is None
        else [
            {
                "kind": "expense",
                **{k: expense[k] for k in ("version", "generation", "enabled", "digest")},
            }
        ]
    )
    return {
        "legal_entity_id": snapshot["scope"]["legal_entity_id"],
        "profile": "internal_records",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "blocked" if any(c["status"] == "blocked" for c in checks) else "not_verified",
        "production_status": "not_verified",
        "checks": checks,
        "versions": versions,
        "configuration_digest": request_digest(snapshot),
        "external_verifications": EXTERNAL_VERIFICATIONS.copy(),
    }


def runtime_checks(runtime: dict[str, Any]) -> list[ReadinessCheck]:
    """只读可用于恢复核对，真实任务、锁和外部装配独立决定阻断。"""
    outcomes = {
        "runtime_mode": runtime["mode"] in {"enabled", "draining", "read_only"},
        "runtime_instance_lock": runtime["instance_lock_lost"] is False,
        "runtime_workers": all(
            runtime[name] == "running" for name in ("receipt_worker", "notification_worker")
        ),
        "runtime_external_workers": runtime["external_workers"] == "absent",
    }
    return [check(code, passed, CHECK_DETAILS[code]) for code, passed in outcomes.items()]
