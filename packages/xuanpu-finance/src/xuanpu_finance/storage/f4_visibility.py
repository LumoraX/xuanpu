"""将现有逐项可见性翻译为无重复行的SQL相关谓词。"""
from __future__ import annotations

import re

# 与FinanceBase._f3_assigned保持相同的制度、渠道、岗位代数和父目标语义。
ASSIGNED = """SELECT EXISTS(SELECT 1 FROM finance_compliance_plan p
 JOIN finance_compliance_workflow w ON(w.tenant_id=p.tenant_id AND w.legal_entity_id=p.legal_entity_id AND w.plan_id=p.id)
 JOIN finance_compliance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id)
 JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('reviewer','approver'))
 JOIN finance_compliance_policy c ON(c.tenant_id=p.tenant_id AND c.legal_entity_id=p.legal_entity_id AND c.enabled AND c.generation=(p.data->'bindings'->'policy'->>'generation')::bigint)
 JOIN finance_compliance_provider v ON(v.tenant_id=p.tenant_id AND v.legal_entity_id=p.legal_entity_id AND v.provider=p.data->'bindings'->'provider'->>'provider' AND v.configuration_version=(p.data->'bindings'->'provider'->>'configuration_version')::integer AND v.generation=(p.data->'bindings'->'provider'->>'generation')::bigint AND v.enabled)
 WHERE p.tenant_id=$1 AND p.legal_entity_id=$2 AND s.approver_user_id=$3 AND p.expires_at>now() AND w.status IN ('under_review','approved') AND ((p.target_type=$4 AND p.target_id=$5)
 OR ($4='invoice' AND EXISTS(SELECT 1 FROM finance_red_letter_case rc
 WHERE rc.tenant_id=p.tenant_id AND rc.legal_entity_id=p.legal_entity_id AND rc.id=p.target_id AND rc.invoice_id=$5))
 OR ($4='invoice' AND EXISTS(SELECT 1 FROM finance_allocation a
 WHERE a.tenant_id=p.tenant_id AND a.legal_entity_id=p.legal_entity_id AND a.document_id=p.target_id AND a.invoice_id=$5))
 OR ($4='invoice' AND EXISTS(SELECT 1 FROM finance_red_letter_link l LEFT
 JOIN finance_allocation a ON(a.tenant_id=l.tenant_id AND a.legal_entity_id=l.legal_entity_id AND a.invoice_id=l.blue_invoice_id)
 WHERE l.tenant_id=p.tenant_id AND l.legal_entity_id=p.legal_entity_id AND l.red_invoice_id=$5 AND (l.blue_invoice_id=p.target_id
 OR a.document_id=p.target_id)))))"""


def assigned(alias: str, kind: str, target: str, user: str) -> str:
    """对当前行插入实际指派的完整父目标权限检查。"""
    values = {"$1": f"{alias}.tenant_id", "$2": f"{alias}.legal_entity_id", "$3": user, "$4": kind, "$5": target}
    return re.sub(r"\$[1-5]", lambda match: values[match.group()], ASSIGNED).removeprefix("SELECT ")


def plan_assignment(alias: str, user: str, *, legacy: bool = False) -> str:
    """计划按实际工作流和未撤销岗位代数提供必要读取权。"""
    prefix = "finance" if legacy else "finance_compliance"
    target = f"w.target_type='action_plan' AND w.target_id={alias}.id" if legacy else f"w.plan_id={alias}.id"
    return f"""EXISTS(SELECT 1 FROM {prefix}_workflow w
      JOIN {prefix}_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id)
      JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('approver','reviewer'))
      WHERE w.tenant_id={alias}.tenant_id AND w.legal_entity_id={alias}.legal_entity_id AND {target} AND s.approver_user_id={user})"""


def visibility(name: str, alias: str = "r", user: str = "$4") -> str:
    """精确复用单记录的公司岗位、本人及递归档案权限，不连接展开候选。"""
    grant = f"g.tenant_id={alias}.tenant_id AND g.legal_entity_id={alias}.legal_entity_id AND g.user_id={user}"
    base = f"g.role='reviewer' OR {alias}.owner_id={user}"
    extra = "false"
    if name == "plans":
        extra = f"{alias}.actor_id={user} OR {plan_assignment(alias, user)}"
    elif name in {"verifications", "confirmations", "red_cases", "adjustments"}:
        extra = assigned(alias, "'invoice'", f"{alias}.invoice_id", user)
    elif name == "archives":
        direct = assigned(alias, f"{alias}.target_type", f"{alias}.target_id", user)
        invoice = target_owner(alias, "finance_invoice", user)
        expense = target_owner(alias, "finance_expense_claim", user)
        extra = f"{direct} OR ({alias}.target_type='invoice' AND {invoice}) OR ({alias}.target_type<>'invoice' AND {expense})"
    elif name in {"audits", "supplements"}:
        parent = visibility("archives", "ar", user)
        extra = f"EXISTS(SELECT 1 FROM finance_archive ar WHERE ar.tenant_id={alias}.tenant_id AND ar.legal_entity_id={alias}.legal_entity_id AND ar.id={alias}.archive_id AND {parent})"
    return f"EXISTS(SELECT 1 FROM finance_grant g WHERE {grant} AND ({base} OR {extra}))"


def target_owner(alias: str, table: str, user: str) -> str:
    """档案保留原发票或费用父目标拥有者的读取权。"""
    return f"EXISTS(SELECT 1 FROM {table} t WHERE t.tenant_id={alias}.tenant_id AND t.legal_entity_id={alias}.legal_entity_id AND t.id={alias}.target_id AND t.owner_id={user})"
