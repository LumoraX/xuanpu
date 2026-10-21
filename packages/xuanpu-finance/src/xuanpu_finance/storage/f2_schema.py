"""F2受控目录、专用审批与资金/凭证持久状态。"""

from __future__ import annotations

F2_SCHEMA = """
ALTER TABLE finance_expense_claim ADD COLUMN IF NOT EXISTS document_kind text NOT NULL DEFAULT 'expense_claim' CHECK(document_kind IN ('expense_claim','payable'));
ALTER TABLE finance_expense_claim ADD COLUMN IF NOT EXISTS simulation boolean NOT NULL DEFAULT false;
ALTER TABLE finance_workflow ADD COLUMN IF NOT EXISTS target_type text NOT NULL DEFAULT 'expense_claim';
ALTER TABLE finance_workflow ADD COLUMN IF NOT EXISTS target_digest text;
ALTER TABLE finance_workflow ADD COLUMN IF NOT EXISTS target_id text;
ALTER TABLE finance_execution_grant DROP CONSTRAINT IF EXISTS finance_execution_grant_scope_check;
ALTER TABLE finance_execution_grant ADD COLUMN IF NOT EXISTS plan_id text;
ALTER TABLE finance_execution_grant ADD COLUMN IF NOT EXISTS plan_digest text;
ALTER TABLE finance_execution_grant ADD COLUMN IF NOT EXISTS amount_limit numeric(18,2);
ALTER TABLE finance_execution_grant ADD COLUMN IF NOT EXISTS expires_at timestamptz;
CREATE TABLE IF NOT EXISTS finance_settlement_mode (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), simulation boolean NOT NULL);
CREATE TABLE IF NOT EXISTS finance_settlement_policy_revision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 digest text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(tenant_id,legal_entity_id,version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_settlement_policy (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 enabled boolean NOT NULL, generation integer NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id),
 FOREIGN KEY(tenant_id,legal_entity_id,version) REFERENCES finance_settlement_policy_revision);
CREATE TABLE IF NOT EXISTS finance_settlement_catalog_revision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, kind text NOT NULL, source text NOT NULL,
 id text NOT NULL, version text NOT NULL, digest text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,kind,source,id,version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_settlement_catalog (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, kind text NOT NULL, source text NOT NULL,
 id text NOT NULL, version text NOT NULL, generation integer NOT NULL DEFAULT 1,
 enabled boolean NOT NULL DEFAULT true, PRIMARY KEY(tenant_id,legal_entity_id,kind,source,id));
CREATE TABLE IF NOT EXISTS finance_settlement_provider (
 provider text NOT NULL, configuration_version integer NOT NULL,
 tenant_id text NOT NULL, legal_entity_id text NOT NULL,
 digest text NOT NULL, data jsonb NOT NULL, enabled boolean NOT NULL,
 generation integer NOT NULL DEFAULT 1,
 PRIMARY KEY(provider,configuration_version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_settlement_operation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 document_id text NOT NULL, plan_id text NOT NULL, action text NOT NULL, target_id text NOT NULL,
 amount numeric(18,2) NOT NULL CHECK(amount>0), status text NOT NULL DEFAULT 'accepted',
 version integer NOT NULL DEFAULT 1, simulation boolean NOT NULL,
 provider text NOT NULL, configuration_version integer NOT NULL, adapter_version text NOT NULL,
 external_request_id text NOT NULL, request jsonb, result jsonb,
 lease_token text, lease_until timestamptz, query_count integer NOT NULL DEFAULT 0,
 next_query_at timestamptz, query_requested boolean NOT NULL DEFAULT false,
 last_error text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,plan_id),
 UNIQUE(provider,configuration_version,external_request_id),
 FOREIGN KEY(tenant_id,legal_entity_id,document_id) REFERENCES finance_expense_claim,
 FOREIGN KEY(tenant_id,legal_entity_id,plan_id) REFERENCES finance_action_plan);
ALTER TABLE finance_settlement_operation ADD COLUMN IF NOT EXISTS document_kind text NOT NULL DEFAULT 'expense_claim';
UPDATE finance_settlement_operation o SET document_kind=c.document_kind FROM finance_expense_claim c WHERE o.tenant_id=c.tenant_id AND o.legal_entity_id=c.legal_entity_id AND o.document_id=c.id AND o.document_kind<>c.document_kind;
CREATE TABLE IF NOT EXISTS finance_settlement_lease (
 lease_token text PRIMARY KEY, tenant_id text NOT NULL, legal_entity_id text NOT NULL,
 operation_id text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_settlement_operation);
DROP TRIGGER IF EXISTS finance_settlement_lease_immutable ON finance_settlement_lease;
CREATE TRIGGER finance_settlement_lease_immutable BEFORE UPDATE OR DELETE ON finance_settlement_lease FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
CREATE TABLE IF NOT EXISTS finance_payment (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, document_id text NOT NULL,
 operation_id text NOT NULL, amount numeric(18,2) NOT NULL, version integer NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'pending', simulation boolean NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 UNIQUE(tenant_id,legal_entity_id,operation_id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_settlement_operation);
CREATE TABLE IF NOT EXISTS finance_posting (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, document_id text NOT NULL,
 operation_id text NOT NULL, amount numeric(18,2) NOT NULL, version integer NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'pending', simulation boolean NOT NULL, original_posting_id text,
 data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,operation_id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_settlement_operation);
CREATE TABLE IF NOT EXISTS finance_settlement_receipt (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, operation_id text NOT NULL,
 raw bytea NOT NULL, sha256 text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_settlement_operation);
CREATE TABLE IF NOT EXISTS finance_settlement_exception (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 operation_id text NOT NULL, version integer NOT NULL DEFAULT 1, rule_id text NOT NULL,
 status text NOT NULL DEFAULT 'open', severity text NOT NULL DEFAULT 'hard_block',
 evidence jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id));
CREATE TABLE IF NOT EXISTS finance_settlement_inbox (
 provider text NOT NULL, configuration_version integer NOT NULL, event_id text NOT NULL,
 raw_hash text NOT NULL, raw bytea NOT NULL, status text NOT NULL, operation_id text,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(provider,configuration_version,event_id));
CREATE INDEX IF NOT EXISTS finance_settlement_pending ON finance_settlement_operation(status,next_query_at,lease_until,created_at);
CREATE INDEX IF NOT EXISTS finance_settlement_document ON finance_settlement_operation(tenant_id,legal_entity_id,document_id,action,status);
DROP TRIGGER IF EXISTS finance_settlement_policy_immutable ON finance_settlement_policy_revision;
CREATE TRIGGER finance_settlement_policy_immutable BEFORE UPDATE OR DELETE ON finance_settlement_policy_revision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_settlement_catalog_immutable ON finance_settlement_catalog_revision;
CREATE TRIGGER finance_settlement_catalog_immutable BEFORE UPDATE OR DELETE ON finance_settlement_catalog_revision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_settlement_receipt_immutable ON finance_settlement_receipt;
CREATE TRIGGER finance_settlement_receipt_immutable BEFORE UPDATE OR DELETE ON finance_settlement_receipt FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
"""

F2_INBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS finance_settlement_inbox_conflict (
 id text PRIMARY KEY, provider text NOT NULL, configuration_version integer NOT NULL,
 event_id text NOT NULL, raw_hash text NOT NULL, raw bytea NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
DROP TRIGGER IF EXISTS finance_inbox_conflict_immutable ON finance_settlement_inbox_conflict;
CREATE TRIGGER finance_inbox_conflict_immutable BEFORE UPDATE OR DELETE ON finance_settlement_inbox_conflict FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
"""
