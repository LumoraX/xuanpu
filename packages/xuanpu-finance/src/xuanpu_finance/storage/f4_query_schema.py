"""F4授权候选、稳定游标和父记录过滤的幂等查询索引。"""
from __future__ import annotations

F4_QUERY_SCHEMA = "\n".join(
    f"CREATE INDEX IF NOT EXISTS f4_{table}_page ON {table}(tenant_id,legal_entity_id,created_at,id);"
    for table in (
        "finance_invoice_verification", "finance_tax_confirmation", "finance_red_letter_case",
        "finance_tax_adjustment", "finance_archive", "finance_archive_audit",
        "finance_archive_supplement", "finance_compliance_plan",
    )
) + """
CREATE INDEX IF NOT EXISTS f4_verification_parent ON finance_invoice_verification(tenant_id,legal_entity_id,invoice_id,created_at,id);
CREATE INDEX IF NOT EXISTS f4_audit_parent ON finance_archive_audit(tenant_id,legal_entity_id,archive_id,created_at,id);
CREATE INDEX IF NOT EXISTS f4_supplement_parent ON finance_archive_supplement(tenant_id,legal_entity_id,archive_id,created_at,id);
CREATE INDEX IF NOT EXISTS f4_compliance_expiry ON finance_compliance_plan(tenant_id,legal_entity_id,expires_at,id);
CREATE INDEX IF NOT EXISTS f4_action_expiry ON finance_action_plan(tenant_id,legal_entity_id,expires_at,id);
CREATE INDEX IF NOT EXISTS f4_compliance_plan_workflow ON finance_compliance_workflow(tenant_id,legal_entity_id,plan_id,id);
CREATE INDEX IF NOT EXISTS f4_compliance_assignee ON finance_compliance_workflow_step(tenant_id,legal_entity_id,approver_user_id,workflow_id,authority_generation);
CREATE INDEX IF NOT EXISTS f4_legacy_assignee ON finance_workflow_step(tenant_id,legal_entity_id,approver_user_id,workflow_id,authority_generation);
"""
