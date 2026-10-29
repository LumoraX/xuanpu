"""统一行动和待办以轻量候选投影合并，正文仅在选页后读取。"""
from __future__ import annotations

from xuanpu_finance.storage.f4_visibility import plan_assignment, visibility


def action_candidates() -> str:
    """两个领域按原到期时间和计划ID稳定排序，权限与目标先入SQL。"""
    current = visibility("plans", user="$3")
    legacy = plan_assignment("r", "$3", legacy=True)
    return f"""
    SELECT r.id,r.expires_at AS stamp,false AS f3 FROM finance_action_plan r
    WHERE r.tenant_id=$1 AND r.legal_entity_id=$2
      AND r.data->>'action'=ANY($5::text[])
      AND ($4::text IS NULL OR r.target_id=$4 OR r.data->>'target_id'=$4)
      AND EXISTS(SELECT 1 FROM finance_grant g WHERE g.tenant_id=r.tenant_id
        AND g.legal_entity_id=r.legal_entity_id AND g.user_id=$3
        AND (r.owner_id=$3 OR g.role='reviewer' OR (g.role='approver' AND {legacy})))
    UNION ALL
    SELECT r.id,r.expires_at AS stamp,true AS f3 FROM finance_compliance_plan r
    WHERE r.tenant_id=$1 AND r.legal_entity_id=$2 AND r.target_type<>'action_plan'
      AND ($4::text IS NULL OR r.target_id=$4) AND {current}
    """


def review_candidates() -> str:
    """用工作流步骤的唯一ID分页并核对当前代数，不读取全量JSON。"""
    return " UNION ALL ".join(review_branch(legacy=legacy) for legacy in (True, False))


def review_branch(*, legacy: bool) -> str:
    """各领域仅投影排序字段和步骤引用，避免JOIN扩展出重复计划。"""
    prefix = "finance" if legacy else "finance_compliance"
    table = "finance_expense_claim" if legacy else "finance_compliance_plan"
    relation = "w.document_id" if legacy else "w.plan_id"
    flag = "false" if legacy else "true"
    return f"""SELECT s.id,r.created_at AS stamp,{flag} AS f3
    FROM {prefix}_workflow_step s
    JOIN {prefix}_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id)
    JOIN {table} r ON(r.tenant_id=w.tenant_id AND r.legal_entity_id=w.legal_entity_id AND r.id={relation})
    JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('approver','reviewer'))
    WHERE s.tenant_id=$1 AND s.legal_entity_id=$2 AND s.approver_user_id=$3
      AND s.status='pending' AND w.status='under_review'"""


def review_record(*, legacy: bool) -> str:
    """仅回读本页所选步骤的正文和既有序列化辅助字段。"""
    prefix = "finance" if legacy else "finance_compliance"
    table = "finance_expense_claim" if legacy else "finance_compliance_plan"
    relation = "w.document_id" if legacy else "w.plan_id"
    fields = "w.due_at,w.target_type,w.target_id AS workflow_target_id" if legacy else "r.expires_at AS due_at"
    return f"""SELECT r.*,s.id AS step_id,s.version AS step_version,s.approver_user_id,
      w.id AS workflow_id,w.version AS workflow_version,{fields}
    FROM {prefix}_workflow_step s
    JOIN {prefix}_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id)
    JOIN {table} r ON(r.tenant_id=w.tenant_id AND r.legal_entity_id=w.legal_entity_id AND r.id={relation})
    WHERE s.tenant_id=$1 AND s.legal_entity_id=$2 AND s.id=$3 LIMIT 1"""
