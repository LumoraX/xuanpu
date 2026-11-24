"""F1报销事务表，与F0导入队列和幂等空间完全独立。"""

from __future__ import annotations

F1_SCHEMA = """
ALTER TABLE finance_grant DROP CONSTRAINT IF EXISTS finance_grant_role_check;
ALTER TABLE finance_grant ADD CONSTRAINT finance_grant_role_check CHECK(role IN ('collector','reviewer','approver'));
CREATE TABLE IF NOT EXISTS finance_grant_epoch (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, user_id text NOT NULL,
 role text, generation integer NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id,user_id),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
INSERT INTO finance_grant_epoch(tenant_id,legal_entity_id,user_id,role)
 SELECT tenant_id,legal_entity_id,user_id,role FROM finance_grant ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS finance_expense_policy_revision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 digest text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(tenant_id,legal_entity_id,version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_expense_policy (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 enabled boolean NOT NULL, PRIMARY KEY(tenant_id,legal_entity_id),
 FOREIGN KEY(tenant_id,legal_entity_id,version) REFERENCES finance_expense_policy_revision);
ALTER TABLE finance_expense_policy ADD COLUMN IF NOT EXISTS generation integer NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS finance_expense_claim (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 version integer NOT NULL CHECK(version>0), status text NOT NULL,
 total numeric(18,2) NOT NULL CHECK(total>0), content jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
ALTER TABLE finance_expense_claim ADD COLUMN IF NOT EXISTS record_mode text NOT NULL DEFAULT 'invoice_backed' CHECK(record_mode IN ('invoice_backed','internal_record'));
CREATE TABLE IF NOT EXISTS finance_expense_evidence (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, document_id text NOT NULL,
 document_version integer NOT NULL, receipt_id text NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,document_id,document_version,receipt_id),
 FOREIGN KEY(tenant_id,legal_entity_id,document_id) REFERENCES finance_expense_claim,
 FOREIGN KEY(tenant_id,legal_entity_id,receipt_id) REFERENCES finance_receipt);
CREATE TABLE IF NOT EXISTS finance_expense_history (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, version integer NOT NULL,
 data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id,version),
 FOREIGN KEY(tenant_id,legal_entity_id,id) REFERENCES finance_expense_claim);
CREATE TABLE IF NOT EXISTS finance_allocation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, document_id text NOT NULL,
 invoice_id text NOT NULL, invoice_version integer NOT NULL, invoice_line_id text,
 version integer NOT NULL DEFAULT 1, amount numeric(18,2) NOT NULL CHECK(amount>0),
 tax numeric(18,2) NOT NULL CHECK(tax>=0 AND tax<=amount), state text NOT NULL CHECK(state IN ('proposed','reserved','consumed','released')),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 FOREIGN KEY(tenant_id,legal_entity_id,document_id) REFERENCES finance_expense_claim,
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice);
CREATE INDEX IF NOT EXISTS finance_allocation_balance ON finance_allocation(tenant_id,legal_entity_id,invoice_id,state);
CREATE TABLE IF NOT EXISTS finance_workflow (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, document_id text NOT NULL,
 target_version integer NOT NULL, version integer NOT NULL DEFAULT 1, status text NOT NULL,
 policy_version integer NOT NULL, policy_digest text NOT NULL, due_at timestamptz NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 FOREIGN KEY(tenant_id,legal_entity_id,document_id) REFERENCES finance_expense_claim);
ALTER TABLE finance_workflow ADD COLUMN IF NOT EXISTS policy_generation integer NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS finance_workflow_step (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, workflow_id text NOT NULL,
 position integer NOT NULL, approver_user_id text NOT NULL, name text NOT NULL,
 status text NOT NULL, version integer NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,workflow_id,position),
 FOREIGN KEY(tenant_id,legal_entity_id,workflow_id) REFERENCES finance_workflow);
ALTER TABLE finance_workflow_step ADD COLUMN IF NOT EXISTS authority_generation integer NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS finance_decision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, workflow_id text NOT NULL,
 step_id text, actor_id text NOT NULL, action text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,workflow_id) REFERENCES finance_workflow);
CREATE UNIQUE INDEX IF NOT EXISTS finance_decision_one_step ON finance_decision(tenant_id,legal_entity_id,step_id) WHERE step_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS finance_action_plan (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 target_id text NOT NULL, digest text NOT NULL, expires_at timestamptz NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 FOREIGN KEY(tenant_id,legal_entity_id,target_id) REFERENCES finance_expense_claim);
CREATE TABLE IF NOT EXISTS finance_execution_grant (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, document_id text NOT NULL,
 workflow_id text NOT NULL, scope text NOT NULL CHECK(scope='expense_approved'),
 target_version integer NOT NULL, policy_version integer NOT NULL, policy_digest text NOT NULL,
 active boolean NOT NULL DEFAULT true, PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,workflow_id) REFERENCES finance_workflow);
ALTER TABLE finance_execution_grant ADD COLUMN IF NOT EXISTS policy_generation integer NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS finance_business_operation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 plan_id text, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,plan_id),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_business_idempotency (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, actor_id text NOT NULL,
 operation_id text NOT NULL, key text NOT NULL, digest text NOT NULL, response jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,actor_id,operation_id,key),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_outbox (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 operation_id text NOT NULL, status text NOT NULL DEFAULT 'pending', data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_business_operation);
ALTER TABLE finance_exception ADD COLUMN IF NOT EXISTS severity text NOT NULL DEFAULT 'hard_block' CHECK(severity IN ('hard_block','policy_block','warning'));
CREATE TABLE IF NOT EXISTS finance_exception_evidence (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, exception_id text NOT NULL, receipt_id text NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,exception_id,receipt_id),
 FOREIGN KEY(tenant_id,legal_entity_id,exception_id) REFERENCES finance_exception,
 FOREIGN KEY(tenant_id,legal_entity_id,receipt_id) REFERENCES finance_receipt);
CREATE TABLE IF NOT EXISTS finance_exception_decision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, exception_id text NOT NULL,
 version integer NOT NULL, actor_id text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(tenant_id,legal_entity_id,exception_id,version),
 FOREIGN KEY(tenant_id,legal_entity_id,exception_id) REFERENCES finance_exception);
CREATE INDEX IF NOT EXISTS finance_expense_page ON finance_expense_claim(tenant_id,legal_entity_id,created_at,id);
CREATE INDEX IF NOT EXISTS finance_step_actor ON finance_workflow_step(tenant_id,legal_entity_id,approver_user_id,status);
CREATE OR REPLACE FUNCTION finance_immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION '财务不可变记录禁止修改'; END; $$;
DROP TRIGGER IF EXISTS finance_policy_immutable ON finance_expense_policy_revision;
CREATE TRIGGER finance_policy_immutable BEFORE UPDATE OR DELETE ON finance_expense_policy_revision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_history_immutable ON finance_expense_history;
CREATE TRIGGER finance_history_immutable BEFORE UPDATE OR DELETE ON finance_expense_history FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_plan_immutable ON finance_action_plan;
CREATE TRIGGER finance_plan_immutable BEFORE UPDATE OR DELETE ON finance_action_plan FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_decision_immutable ON finance_decision;
CREATE TRIGGER finance_decision_immutable BEFORE UPDATE OR DELETE ON finance_decision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_exception_decision_immutable ON finance_exception_decision;
CREATE TRIGGER finance_exception_decision_immutable BEFORE UPDATE OR DELETE ON finance_exception_decision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
"""
