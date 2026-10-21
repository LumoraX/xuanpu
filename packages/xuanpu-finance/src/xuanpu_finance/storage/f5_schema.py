"""F5受控单据类型、不可变资金制度和原凭据共享额度。"""
from __future__ import annotations

F5_SCHEMA = """
ALTER TABLE finance_expense_claim DROP CONSTRAINT IF EXISTS finance_expense_claim_document_kind_check;
ALTER TABLE finance_expense_claim ADD CONSTRAINT finance_expense_claim_document_kind_check CHECK(document_kind IN ('expense_claim','payable','funding_case'));
ALTER TABLE finance_expense_claim DROP CONSTRAINT IF EXISTS finance_expense_claim_record_mode_check;
ALTER TABLE finance_expense_claim ADD CONSTRAINT finance_expense_claim_record_mode_check CHECK(record_mode IN ('invoice_backed','internal_record','prepayment_basis','return_reconciliation'));
CREATE TABLE IF NOT EXISTS finance_funding_policy_revision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 digest text NOT NULL, data jsonb NOT NULL, PRIMARY KEY(tenant_id,legal_entity_id,version),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_funding_policy (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, version integer NOT NULL,
 enabled boolean NOT NULL, generation integer NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id),
 FOREIGN KEY(tenant_id,legal_entity_id,version) REFERENCES finance_funding_policy_revision);
CREATE TABLE IF NOT EXISTS finance_funding_authority (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 kind text NOT NULL, scope_digest text NOT NULL, body_digest text NOT NULL,
 evidence_digest text NOT NULL, data jsonb NOT NULL,
 version integer NOT NULL DEFAULT 1, revoked boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(tenant_id,legal_entity_id,id),
 UNIQUE(id), UNIQUE(tenant_id,legal_entity_id,kind,scope_digest),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_funding_original (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, kind text NOT NULL,
 sha256 text NOT NULL, authority_id text NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,kind,sha256),
 FOREIGN KEY(tenant_id,legal_entity_id,authority_id) REFERENCES finance_funding_authority);
CREATE TABLE IF NOT EXISTS finance_funding_case (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, document_id text NOT NULL,
 authority_id text, source_case_id text, financial_version integer NOT NULL DEFAULT 1,
 PRIMARY KEY(tenant_id,legal_entity_id,document_id),
 FOREIGN KEY(tenant_id,legal_entity_id,document_id) REFERENCES finance_expense_claim,
 FOREIGN KEY(tenant_id,legal_entity_id,authority_id) REFERENCES finance_funding_authority,
 FOREIGN KEY(tenant_id,legal_entity_id,source_case_id) REFERENCES finance_funding_case);
CREATE TABLE IF NOT EXISTS finance_funding_reservation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, document_id text NOT NULL,
 authority_id text, source_case_id text, kind text NOT NULL,
 amount numeric(18,2) NOT NULL CHECK(amount>0), state text NOT NULL CHECK(state IN ('reserved','completed','released')),
 decision_id text, PRIMARY KEY(tenant_id,legal_entity_id,document_id),
 FOREIGN KEY(tenant_id,legal_entity_id,document_id) REFERENCES finance_funding_case,
 FOREIGN KEY(tenant_id,legal_entity_id,authority_id) REFERENCES finance_funding_authority,
 FOREIGN KEY(tenant_id,legal_entity_id,source_case_id) REFERENCES finance_funding_case);
CREATE TABLE IF NOT EXISTS finance_funding_authority_decision (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 authority_id text NOT NULL, actor_id text NOT NULL, expected_version integer NOT NULL,
 reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,authority_id) REFERENCES finance_funding_authority);
CREATE INDEX IF NOT EXISTS finance_funding_authority_balance ON finance_funding_reservation(tenant_id,legal_entity_id,authority_id,state);
CREATE INDEX IF NOT EXISTS finance_funding_parent_balance ON finance_funding_reservation(tenant_id,legal_entity_id,source_case_id,state);
CREATE INDEX IF NOT EXISTS finance_funding_list ON finance_expense_claim(tenant_id,legal_entity_id,document_kind,status,created_at,id);
CREATE OR REPLACE FUNCTION finance_funding_authority_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable funding authority'; END IF;
 IF (NEW.tenant_id,NEW.legal_entity_id,NEW.id,NEW.kind,NEW.scope_digest,NEW.body_digest,NEW.evidence_digest,NEW.data,NEW.created_at) IS DISTINCT FROM (OLD.tenant_id,OLD.legal_entity_id,OLD.id,OLD.kind,OLD.scope_digest,OLD.body_digest,OLD.evidence_digest,OLD.data,OLD.created_at) OR OLD.revoked OR NOT NEW.revoked OR NEW.version<>OLD.version+1 THEN
 RAISE EXCEPTION 'immutable funding authority'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS finance_funding_authority_immutable ON finance_funding_authority;
CREATE TRIGGER finance_funding_authority_immutable BEFORE UPDATE OR DELETE ON finance_funding_authority FOR EACH ROW EXECUTE FUNCTION finance_funding_authority_guard();
DROP TRIGGER IF EXISTS finance_funding_policy_immutable ON finance_funding_policy_revision;
CREATE TRIGGER finance_funding_policy_immutable BEFORE UPDATE OR DELETE ON finance_funding_policy_revision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_funding_original_immutable ON finance_funding_original;
CREATE TRIGGER finance_funding_original_immutable BEFORE UPDATE OR DELETE ON finance_funding_original FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
DROP TRIGGER IF EXISTS finance_funding_authority_decision_immutable ON finance_funding_authority_decision;
CREATE TRIGGER finance_funding_authority_decision_immutable BEFORE UPDATE OR DELETE ON finance_funding_authority_decision FOR EACH ROW EXECUTE FUNCTION finance_immutable_record();
CREATE OR REPLACE FUNCTION finance_funding_reservation_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF OLD.state='completed' THEN RAISE EXCEPTION 'immutable finalized funding reservation'; END IF;
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable funding reservation history'; END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS finance_funding_reservation_finalized ON finance_funding_reservation;
CREATE TRIGGER finance_funding_reservation_finalized BEFORE UPDATE OR DELETE ON finance_funding_reservation FOR EACH ROW EXECUTE FUNCTION finance_funding_reservation_guard();
"""
