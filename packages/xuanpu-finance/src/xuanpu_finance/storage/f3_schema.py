"""独立F3目标、审批、外查及不可变封存事实的幂等结构。"""

from __future__ import annotations

F3_SCHEMA = """
ALTER TABLE finance_operation ADD COLUMN IF NOT EXISTS source_simulation boolean NOT NULL DEFAULT false;
ALTER TABLE finance_invoice ADD COLUMN IF NOT EXISTS source_simulation boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS finance_compliance_mode (
 singleton boolean PRIMARY KEY CHECK(singleton), simulation boolean NOT NULL);
CREATE TABLE IF NOT EXISTS finance_compliance_policy_revision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 digest text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(tenant_id,legal_entity_id,version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_compliance_policy (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 enabled boolean NOT NULL, generation bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id),
 FOREIGN KEY(tenant_id,legal_entity_id,version) REFERENCES finance_compliance_policy_revision);
CREATE TABLE IF NOT EXISTS finance_compliance_provider (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, provider text NOT NULL,
 configuration_version integer NOT NULL, kind text NOT NULL CHECK(kind IN ('tax','archive')),
 enabled boolean NOT NULL, generation bigint NOT NULL DEFAULT 1, digest text NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(provider,configuration_version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_invoice_compliance (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, invoice_id text NOT NULL,
 version integer NOT NULL DEFAULT 1, authority_revision bigint NOT NULL DEFAULT 0,
 verification_status text NOT NULL DEFAULT 'not_checked', invoice_status text NOT NULL DEFAULT 'normal',
 valid_until timestamptz, snapshot jsonb, verification_evidence jsonb,
 PRIMARY KEY(tenant_id,legal_entity_id,invoice_id),
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice);
ALTER TABLE finance_invoice_compliance ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE finance_invoice_compliance ADD COLUMN IF NOT EXISTS configuration_version integer;
ALTER TABLE finance_invoice_compliance ADD COLUMN IF NOT EXISTS provider_generation bigint;
CREATE TABLE IF NOT EXISTS finance_invoice_authority_revision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, invoice_id text NOT NULL,
 provider text NOT NULL, configuration_version integer NOT NULL, provider_generation bigint NOT NULL,
 authority_revision bigint NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,invoice_id,provider,configuration_version,provider_generation));
CREATE TABLE IF NOT EXISTS finance_compliance_plan (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 actor_id text NOT NULL, owner_id text NOT NULL, target_type text NOT NULL,
 target_id text NOT NULL, target_version integer NOT NULL, digest text NOT NULL,
 expires_at timestamptz NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 CHECK(target_type IN ('invoice','red_letter_case','archive','expense_claim','payable','funding_case','action_plan')),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_compliance_workflow (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 plan_id text NOT NULL, version integer NOT NULL DEFAULT 1, status text NOT NULL DEFAULT 'under_review',
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,plan_id) REFERENCES finance_compliance_plan);
CREATE TABLE IF NOT EXISTS finance_compliance_workflow_step (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 workflow_id text NOT NULL, position integer NOT NULL, approver_user_id text NOT NULL,
 authority_generation bigint NOT NULL, status text NOT NULL, version integer NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,workflow_id) REFERENCES finance_compliance_workflow);
CREATE TABLE IF NOT EXISTS finance_compliance_decision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 workflow_id text NOT NULL, step_id text NOT NULL, actor_id text NOT NULL,
 action text NOT NULL, plan_digest text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id));
CREATE TABLE IF NOT EXISTS finance_compliance_grant (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, plan_id text NOT NULL,
 plan_digest text NOT NULL, active boolean NOT NULL DEFAULT true,
 PRIMARY KEY(tenant_id,legal_entity_id,plan_id),
 FOREIGN KEY(tenant_id,legal_entity_id,plan_id) REFERENCES finance_compliance_plan);
CREATE TABLE IF NOT EXISTS finance_compliance_operation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 owner_id text NOT NULL, target_type text NOT NULL, target_id text NOT NULL, plan_id text NOT NULL,
 action text NOT NULL, version integer NOT NULL DEFAULT 1, status text NOT NULL DEFAULT 'accepted',
 business_result text NOT NULL DEFAULT 'pending', simulation boolean NOT NULL,
 provider text NOT NULL, configuration_version integer NOT NULL, adapter_version text NOT NULL,
 external_request_id text NOT NULL UNIQUE, request_template jsonb NOT NULL, request jsonb,
 lease_token text, lease_until timestamptz, next_query_at timestamptz, query_count integer NOT NULL DEFAULT 0,
 query_requested boolean NOT NULL DEFAULT false, last_error text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 FOREIGN KEY(tenant_id,legal_entity_id,plan_id) REFERENCES finance_compliance_plan);
CREATE TABLE IF NOT EXISTS finance_compliance_lease (
 lease_token text PRIMARY KEY, tenant_id text NOT NULL, legal_entity_id text NOT NULL, operation_id text NOT NULL,
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_compliance_operation);
CREATE TABLE IF NOT EXISTS finance_compliance_receipt (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, operation_id text NOT NULL,
 raw bytea NOT NULL, sha256 text NOT NULL, data jsonb NOT NULL, stale boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_compliance_operation);
CREATE TABLE IF NOT EXISTS finance_invoice_verification (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 invoice_id text NOT NULL, receipt_id text NOT NULL, authority_revision bigint NOT NULL,
 data jsonb NOT NULL, applied boolean NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice);
ALTER TABLE finance_invoice_verification ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE finance_invoice_verification ADD COLUMN IF NOT EXISTS configuration_version integer;
ALTER TABLE finance_invoice_verification ADD COLUMN IF NOT EXISTS provider_generation bigint;
CREATE TABLE IF NOT EXISTS finance_tax_confirmation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 invoice_id text NOT NULL, operation_id text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id));
CREATE TABLE IF NOT EXISTS finance_red_letter_case (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 invoice_id text NOT NULL, external_case_id text NOT NULL, version integer NOT NULL DEFAULT 1,
 authority_revision bigint NOT NULL, status text NOT NULL, data jsonb NOT NULL, red_invoice_id text,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 UNIQUE(tenant_id,legal_entity_id,external_case_id),
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice);
ALTER TABLE finance_red_letter_case ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE finance_red_letter_case ADD COLUMN IF NOT EXISTS configuration_version integer;
ALTER TABLE finance_red_letter_case ADD COLUMN IF NOT EXISTS provider_generation bigint;
CREATE TABLE IF NOT EXISTS finance_red_letter_link (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, case_id text NOT NULL,
 blue_invoice_id text NOT NULL, red_invoice_id text NOT NULL, actor_id text NOT NULL,
 amount numeric(18,2) NOT NULL CHECK(amount>0), tax numeric(18,2) NOT NULL CHECK(tax>=0),
 data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id),
 UNIQUE(tenant_id,legal_entity_id,red_invoice_id), UNIQUE(tenant_id,legal_entity_id,case_id));
CREATE TABLE IF NOT EXISTS finance_tax_adjustment (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 invoice_id text NOT NULL, external_adjustment_id text NOT NULL, version integer NOT NULL DEFAULT 1,
 status text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,external_adjustment_id));
CREATE TABLE IF NOT EXISTS finance_tax_adjustment_evidence (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, adjustment_id text NOT NULL,
 actor_id text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id));
CREATE TABLE IF NOT EXISTS finance_archive (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 target_type text NOT NULL, target_id text NOT NULL, operation_id text NOT NULL, version integer NOT NULL DEFAULT 1,
 manifest_hash text NOT NULL, source_snapshot_hash text NOT NULL, parent_manifest_hash text,
 snapshot jsonb NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,operation_id),
 UNIQUE(tenant_id,legal_entity_id,manifest_hash));
CREATE TABLE IF NOT EXISTS finance_archive_health (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, archive_id text NOT NULL,
 supplement_required boolean NOT NULL DEFAULT false, last_audit_status text,
 PRIMARY KEY(tenant_id,legal_entity_id,archive_id),
 FOREIGN KEY(tenant_id,legal_entity_id,archive_id) REFERENCES finance_archive);
CREATE TABLE IF NOT EXISTS finance_archive_audit (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 archive_id text NOT NULL, operation_id text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id));
CREATE TABLE IF NOT EXISTS finance_archive_supplement (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 archive_id text NOT NULL, child_archive_id text NOT NULL, source_event_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id),
 UNIQUE(tenant_id,legal_entity_id,archive_id,source_event_id));
CREATE TABLE IF NOT EXISTS finance_compliance_inbox (
 provider text NOT NULL, configuration_version integer NOT NULL, event_id text NOT NULL,
 raw_hash text NOT NULL, raw bytea NOT NULL, status text NOT NULL, operation_id text,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(provider,configuration_version,event_id));
CREATE TABLE IF NOT EXISTS finance_compliance_inbox_conflict (
 id text PRIMARY KEY, provider text NOT NULL, configuration_version integer NOT NULL,
 event_id text NOT NULL, raw_hash text NOT NULL, raw bytea NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS finance_compliance_exception (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 operation_id text, rule_id text NOT NULL, data jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id));
CREATE INDEX IF NOT EXISTS finance_compliance_pending ON finance_compliance_operation(status,next_query_at,lease_until,created_at);
"""

F3_SCHEMA += """
ALTER TABLE finance_compliance_plan DROP CONSTRAINT IF EXISTS finance_compliance_plan_target_type_check;
ALTER TABLE finance_compliance_plan ADD CONSTRAINT finance_compliance_plan_target_type_check CHECK(target_type IN ('invoice','red_letter_case','archive','expense_claim','payable','funding_case','action_plan'));
"""

IMMUTABLE_TABLES = (
    "finance_compliance_policy_revision", "finance_compliance_plan", "finance_compliance_decision",
    "finance_compliance_receipt", "finance_invoice_verification", "finance_tax_confirmation",
    "finance_red_letter_link", "finance_tax_adjustment_evidence", "finance_archive",
    "finance_archive_audit", "finance_archive_supplement", "finance_compliance_inbox_conflict",
)


def immutable_schema() -> str:
    """为全部事实历史安装拒绝更新和删除的数据库约束。"""
    return "\n".join(
        f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}; CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();"
        for table in IMMUTABLE_TABLES
    )
