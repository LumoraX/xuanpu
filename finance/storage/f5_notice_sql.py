"""只从已确定付款和实际未结余额生成原预付到票提醒。"""
from __future__ import annotations

FUNDING_DUE_SQL = """
 UNION ALL
 SELECT c.tenant_id,c.legal_entity_id,'funding_due',c.id,
 concat(c.version,':',f.financial_version,':',paid.amount,':',closed.amount),c.owner_id,
 'funding_due','预付到票或余款核对已逾期','funding_case',c.id,'awaiting_invoice'
 FROM finance_expense_claim c
 JOIN finance_funding_case f ON(f.tenant_id=c.tenant_id AND f.legal_entity_id=c.legal_entity_id AND f.document_id=c.id)
 LEFT JOIN LATERAL (SELECT COALESCE(sum(o.amount),0) AS amount FROM finance_settlement_operation o
   WHERE o.tenant_id=c.tenant_id AND o.legal_entity_id=c.legal_entity_id AND o.document_id=c.id
   AND o.action='execute_payment' AND o.status='succeeded') paid ON true
 LEFT JOIN LATERAL (SELECT COALESCE(sum(r.amount),0) AS amount FROM finance_funding_reservation r
   WHERE r.tenant_id=c.tenant_id AND r.legal_entity_id=c.legal_entity_id AND r.source_case_id=c.id
   AND r.state='completed') closed ON true
 WHERE c.document_kind='funding_case' AND c.content->>'scenario'='prepayment'
 AND c.status='approved' AND (c.content->>'due_date')::date < (now() AT TIME ZONE 'Asia/Shanghai')::date
 AND paid.amount>closed.amount
"""
