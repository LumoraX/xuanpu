"""仅从真实财务资源投影收件人及固定少敏摘要。"""

from __future__ import annotations

from xuanpu_finance.storage.f5_notice_sql import FUNDING_DUE_SQL

# 与F3当前档案补包闭包一致，仅读真实未补事件；审计ID集合代表真实健康历史。
ARCHIVE_STATE_SQL = """
LEFT JOIN LATERAL (
 WITH RECURSIVE funding_links(a_id,b_id) AS (
  SELECT document_id,source_case_id FROM finance_funding_case WHERE tenant_id=a.tenant_id AND legal_entity_id=a.legal_entity_id AND source_case_id IS NOT NULL
  UNION SELECT source_case_id,document_id FROM finance_funding_case WHERE tenant_id=a.tenant_id AND legal_entity_id=a.legal_entity_id AND source_case_id IS NOT NULL
 ), documents(id) AS (
  SELECT a.target_id WHERE a.target_type<>'invoice'
  UNION SELECT x.document_id FROM finance_allocation x WHERE x.tenant_id=a.tenant_id AND x.legal_entity_id=a.legal_entity_id AND a.target_type='invoice' AND x.invoice_id=a.target_id
 ), family(id) AS (
  SELECT id FROM documents
  UNION SELECT l.b_id FROM funding_links l JOIN family f ON(l.a_id=f.id)
 ), invoice_roots(id) AS (
  SELECT a.target_id WHERE a.target_type='invoice'
  UNION SELECT x.invoice_id FROM finance_allocation x WHERE x.tenant_id=a.tenant_id AND x.legal_entity_id=a.legal_entity_id AND x.document_id IN (SELECT id FROM family)
 ), invoices(id) AS (
  SELECT id FROM invoice_roots
  UNION SELECT l.red_invoice_id FROM finance_red_letter_link l JOIN invoices i ON(l.blue_invoice_id=i.id) WHERE l.tenant_id=a.tenant_id AND l.legal_entity_id=a.legal_entity_id
 ), aggregates AS (
  SELECT a.target_id AS id
  UNION SELECT jsonb_array_elements_text(a.snapshot->'snapshots'->'aggregate_ids')
  UNION SELECT id FROM invoices
  UNION SELECT id FROM family
 )
 SELECT count(*) AS change_count,md5(COALESCE(string_agg(e.sequence::text,',' ORDER BY e.sequence),'')) AS fingerprint
 FROM finance_event e WHERE e.tenant_id=a.tenant_id AND e.legal_entity_id=a.legal_entity_id AND e.aggregate_id IN (SELECT id FROM aggregates)
 AND e.sequence>COALESCE((a.snapshot->'snapshots'->>'event_cutoff_sequence')::bigint,(SELECT max((entry->>'id')::bigint) FROM jsonb_array_elements(a.snapshot->'snapshots'->'events') entry),0)
 AND e.kind NOT LIKE 'compliance.archive%'
 AND NOT EXISTS(SELECT 1 FROM finance_archive_supplement z WHERE z.tenant_id=a.tenant_id AND z.legal_entity_id=a.legal_entity_id AND z.archive_id=a.id AND z.source_event_id=e.sequence::text)
) changes ON true
LEFT JOIN LATERAL (
 SELECT md5(COALESCE(string_agg(au.id,',' ORDER BY au.id),'')) AS fingerprint FROM finance_archive_audit au
 WHERE au.tenant_id=a.tenant_id AND au.legal_entity_id=a.legal_entity_id AND au.archive_id=a.id
) audits ON true
"""

# 所有候选都先绑定当前公司grant，审批来源再绑定不可复活岗位代数。
SOURCE_SQL = (
    """
WITH candidates AS (
 SELECT e.tenant_id,e.legal_entity_id,'exception'::text AS source_type,e.id AS source_id,
 concat(e.version,':',e.status) AS source_revision,e.owner_id AS recipient_id,'exception_open'::text AS kind,
 '财务材料需要补充'::text AS title,'exception'::text AS target_type,e.id AS target_id,e.status AS source_status
 FROM finance_exception e WHERE e.status NOT IN ('resolved','accepted')
 UNION ALL
 SELECT s.tenant_id,s.legal_entity_id,'legacy_step',s.id,
 concat(s.version,':',w.version,':',s.authority_generation,':',w.due_at<now()),s.approver_user_id,
 CASE WHEN w.due_at<now() THEN 'approval_overdue' ELSE 'approval_pending' END,
 CASE WHEN w.due_at<now() THEN '财务审批已逾期，请核对原流程' ELSE '有财务事项等待您审批' END,
 CASE WHEN w.target_type='action_plan' THEN 'action_plan' ELSE d.document_kind END,
 CASE WHEN w.target_type='action_plan' THEN w.target_id ELSE w.document_id END,
 CASE WHEN w.due_at<now() THEN 'overdue' ELSE 'pending' END
 FROM finance_workflow_step s JOIN finance_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id)
 JOIN finance_expense_claim d ON(d.tenant_id=w.tenant_id AND d.legal_entity_id=w.legal_entity_id AND d.id=w.document_id)
 JOIN finance_grant_epoch g ON(g.tenant_id=s.tenant_id AND g.legal_entity_id=s.legal_entity_id AND g.user_id=s.approver_user_id AND g.generation=s.authority_generation AND g.role IN ('approver','reviewer'))
 LEFT JOIN finance_expense_policy p ON(p.tenant_id=w.tenant_id AND p.legal_entity_id=w.legal_entity_id)
 LEFT JOIN finance_funding_policy fp ON(fp.tenant_id=w.tenant_id AND fp.legal_entity_id=w.legal_entity_id)
 LEFT JOIN finance_settlement_policy sp ON(sp.tenant_id=w.tenant_id AND sp.legal_entity_id=w.legal_entity_id)
 LEFT JOIN finance_action_plan ap ON(ap.tenant_id=w.tenant_id AND ap.legal_entity_id=w.legal_entity_id AND ap.id=w.target_id)
 WHERE s.status='pending' AND w.status='under_review'
 AND NOT EXISTS(SELECT 1 FROM finance_workflow_step prev WHERE prev.tenant_id=s.tenant_id AND prev.legal_entity_id=s.legal_entity_id AND prev.workflow_id=s.workflow_id AND prev.position<s.position AND prev.status<>'approved')
 AND ((w.target_type<>'action_plan' AND d.version=w.target_version AND d.status='under_review' AND ((d.document_kind='funding_case' AND fp.enabled AND fp.version=w.policy_version AND fp.generation=w.policy_generation) OR (d.document_kind<>'funding_case' AND p.enabled AND p.version=w.policy_version AND p.generation=w.policy_generation)))
 OR (w.target_type='action_plan' AND ap.digest=w.target_digest AND sp.enabled AND sp.version=w.policy_version AND sp.generation=w.policy_generation AND (ap.data->'bindings'->>'document_version')::integer=d.version
 AND (d.document_kind<>'funding_case' OR (fp.enabled AND fp.version=(ap.data#>>'{bindings,business,funding,policy,version}')::integer AND fp.generation=(ap.data#>>'{bindings,business,funding,policy,generation}')::integer))
 AND EXISTS(SELECT 1 FROM finance_settlement_provider v WHERE v.provider=ap.data->'bindings'->'provider'->>'provider' AND v.configuration_version=(ap.data->'bindings'->'provider'->>'configuration_version')::integer AND v.tenant_id=ap.tenant_id AND v.legal_entity_id=ap.legal_entity_id AND v.enabled AND v.generation=(ap.data->'bindings'->'provider'->>'generation')::integer)))
 UNION ALL
 SELECT s.tenant_id,s.legal_entity_id,'compliance_step',s.id,
 concat(s.version,':',w.version,':',s.authority_generation,':',p.expires_at<now()),s.approver_user_id,
 CASE WHEN p.expires_at<now() THEN 'approval_overdue' ELSE 'approval_pending' END,
 CASE WHEN p.expires_at<now() THEN '财务审批已逾期，请核对原计划' ELSE '有财务事项等待您审批' END,
 'action_plan',p.id,CASE WHEN p.expires_at<now() THEN 'overdue' ELSE 'pending' END
 FROM finance_compliance_workflow_step s JOIN finance_compliance_workflow w ON(w.tenant_id=s.tenant_id AND w.legal_entity_id=s.legal_entity_id AND w.id=s.workflow_id)
 JOIN finance_compliance_plan p ON(p.tenant_id=w.tenant_id AND p.legal_entity_id=w.legal_entity_id AND p.id=w.plan_id)
 JOIN finance_grant_epoch g ON(g.tenant_id=s.tenant_id AND g.legal_entity_id=s.legal_entity_id AND g.user_id=s.approver_user_id AND g.generation=s.authority_generation AND g.role IN ('approver','reviewer'))
 JOIN finance_compliance_policy c ON(c.tenant_id=p.tenant_id AND c.legal_entity_id=p.legal_entity_id AND c.enabled AND c.generation=(p.data->'bindings'->'policy'->>'generation')::bigint)
 JOIN finance_compliance_provider v ON(v.tenant_id=p.tenant_id AND v.legal_entity_id=p.legal_entity_id AND v.provider=p.data->'bindings'->'provider'->>'provider' AND v.configuration_version=(p.data->'bindings'->'provider'->>'configuration_version')::integer AND v.generation=(p.data->'bindings'->'provider'->>'generation')::bigint AND v.enabled)
 WHERE s.status='pending' AND w.status='under_review'
 AND NOT EXISTS(SELECT 1 FROM finance_compliance_workflow_step prev WHERE prev.tenant_id=s.tenant_id AND prev.legal_entity_id=s.legal_entity_id AND prev.workflow_id=s.workflow_id AND prev.position<s.position AND prev.status<>'approved')
 UNION ALL
 SELECT o.tenant_id,o.legal_entity_id,'settlement_unknown',o.id,o.version::text,o.owner_id,
 'outcome_unknown','财务任务结果待核对，请恢复原任务','operation',o.id,o.status
 FROM finance_settlement_operation o JOIN finance_expense_claim d ON(d.tenant_id=o.tenant_id AND d.legal_entity_id=o.legal_entity_id AND d.id=o.document_id) JOIN finance_grant g ON(g.tenant_id=o.tenant_id AND g.legal_entity_id=o.legal_entity_id AND g.user_id=o.owner_id) WHERE o.status='outcome_unknown' AND (d.owner_id=o.owner_id OR g.role='reviewer')
 UNION ALL
 SELECT o.tenant_id,o.legal_entity_id,'compliance_unknown',o.id,o.version::text,o.owner_id,
 'outcome_unknown','财务任务结果待核对，请恢复原任务','operation',o.id,o.status
 FROM finance_compliance_operation o WHERE o.status='outcome_unknown'
 UNION ALL
 SELECT r.tenant_id,r.legal_entity_id,'red_deadline',r.id,
 concat(r.version,':',LEAST((r.data->>'initiated_at')::timestamptz+interval '72 hours',(r.data->>'authority_deadline')::timestamptz)<now()),g.user_id,
 'red_deadline','红字确认期限需要核对','red_letter_case',r.id,
 CASE WHEN LEAST((r.data->>'initiated_at')::timestamptz+interval '72 hours',(r.data->>'authority_deadline')::timestamptz)<now() THEN 'expired_pending_check' ELSE 'awaiting_confirmation' END
 FROM finance_red_letter_case r JOIN finance_compliance_policy c ON(c.tenant_id=r.tenant_id AND c.legal_entity_id=r.legal_entity_id)
 JOIN finance_compliance_policy_revision p ON(p.tenant_id=c.tenant_id AND p.legal_entity_id=c.legal_entity_id AND p.version=c.version)
 JOIN finance_grant g ON(g.tenant_id=r.tenant_id AND g.legal_entity_id=r.legal_entity_id AND g.role='reviewer' AND p.data->'tax_executor_user_ids' ? g.user_id)
 WHERE r.status='awaiting_confirmation' AND c.enabled AND p.data->>'tax_enabled'='true'
 UNION ALL
 SELECT a.tenant_id,a.legal_entity_id,'archive_health',a.id,
 concat(a.version,':',h.supplement_required,':',h.last_audit_status,':',changes.fingerprint,':',audits.fingerprint),g.user_id,
 'archive_attention','财务档案需要核对或补充封存','archive',a.id,'attention_required'
 FROM finance_archive a JOIN finance_archive_health h ON(h.tenant_id=a.tenant_id AND h.legal_entity_id=a.legal_entity_id AND h.archive_id=a.id)
"""
    + ARCHIVE_STATE_SQL
    + """
 JOIN finance_compliance_policy c ON(c.tenant_id=a.tenant_id AND c.legal_entity_id=a.legal_entity_id)
 JOIN finance_compliance_policy_revision p ON(p.tenant_id=c.tenant_id AND p.legal_entity_id=c.legal_entity_id AND p.version=c.version)
 JOIN finance_grant g ON(g.tenant_id=a.tenant_id AND g.legal_entity_id=a.legal_entity_id AND g.role='reviewer' AND p.data->'archive_executor_user_ids' ? g.user_id)
 WHERE c.enabled AND p.data->>'archive_enabled'='true'
 AND (h.supplement_required OR h.last_audit_status IN ('failed','unavailable') OR changes.change_count>0)
"""
    + FUNDING_DUE_SQL
    + """
)
SELECT c.*,jsonb_build_array(c.source_type,c.tenant_id,c.legal_entity_id,c.source_id,c.recipient_id)::text AS scan_key
FROM candidates c JOIN finance_grant g ON(g.tenant_id=c.tenant_id AND g.legal_entity_id=c.legal_entity_id AND g.user_id=c.recipient_id)
"""
)

# 原操作的关联单据也须当前可见；原审批经办降级后不能绕过资源授权。
OUTBOX_ACCESS_SQL = """
EXISTS(SELECT 1 FROM finance_grant g WHERE g.tenant_id=o.tenant_id AND g.legal_entity_id=o.legal_entity_id AND g.user_id=o.owner_id
AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(o.data->'resources') resource
WHERE NOT (
(g.role='reviewer') OR
(resource->>'kind' IN ('expense_claim','payable','funding_case') AND EXISTS(
 SELECT 1 FROM finance_expense_claim d WHERE d.tenant_id=o.tenant_id AND d.legal_entity_id=o.legal_entity_id AND d.id=resource->>'id'
 AND (d.owner_id=o.owner_id OR (g.role='approver' AND EXISTS(
 SELECT 1 FROM finance_workflow w JOIN finance_workflow_step st ON(st.tenant_id=w.tenant_id AND st.legal_entity_id=w.legal_entity_id AND st.workflow_id=w.id)
 JOIN finance_grant_epoch ep ON(ep.tenant_id=st.tenant_id AND ep.legal_entity_id=st.legal_entity_id AND ep.user_id=st.approver_user_id AND ep.generation=st.authority_generation AND ep.role IN ('reviewer','approver'))
 WHERE w.tenant_id=d.tenant_id AND w.legal_entity_id=d.legal_entity_id AND w.document_id=d.id AND st.approver_user_id=o.owner_id))))) OR
(resource->>'kind'='exception' AND EXISTS(SELECT 1 FROM finance_exception e WHERE e.tenant_id=o.tenant_id AND e.legal_entity_id=o.legal_entity_id AND e.id=resource->>'id' AND e.owner_id=o.owner_id)) OR
(resource->>'kind'='action_plan' AND EXISTS(SELECT 1 FROM finance_action_plan p WHERE p.tenant_id=o.tenant_id AND p.legal_entity_id=o.legal_entity_id AND p.id=resource->>'id' AND p.owner_id=o.owner_id))
)))
"""

# 当前来源仍可读才显示通知；审批资格及来源代数由同一来源查询复核。
VISIBLE_SQL = (
    """
n.tenant_id=$1 AND n.legal_entity_id=$2 AND n.recipient_id=$3
AND EXISTS(SELECT 1 FROM finance_grant g WHERE g.tenant_id=n.tenant_id AND g.legal_entity_id=n.legal_entity_id AND g.user_id=n.recipient_id)
AND ((n.source_type='outbox' AND EXISTS(SELECT 1 FROM finance_business_operation o WHERE o.tenant_id=n.tenant_id AND o.legal_entity_id=n.legal_entity_id AND o.id=n.target_id AND o.owner_id=n.recipient_id AND """
    + OUTBOX_ACCESS_SQL
    + """))
OR EXISTS(SELECT 1 FROM ("""
    + SOURCE_SQL
    + """) s WHERE s.tenant_id=n.tenant_id AND s.legal_entity_id=n.legal_entity_id AND s.source_type=n.source_type AND s.source_id=n.source_id AND s.recipient_id=n.recipient_id
AND s.source_revision=n.source_revision))
"""
)
