"""独立财务数据库结构；不引用平台身份、文件或业务表。"""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS finance_company (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, name text NOT NULL, tax_id text NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id, legal_entity_id));
CREATE TABLE IF NOT EXISTS finance_grant (
 tenant_id text NOT NULL, user_id text NOT NULL, legal_entity_id text NOT NULL,
 role text NOT NULL CHECK(role IN ('collector','reviewer')),
 PRIMARY KEY(tenant_id,user_id,legal_entity_id),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_operation (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 owner_id text NOT NULL, idempotency_key text NOT NULL, request_hash text NOT NULL,
 version integer NOT NULL DEFAULT 1, status text NOT NULL DEFAULT 'accepted',
 business_result text NOT NULL DEFAULT 'pending', trace_id text NOT NULL,
 source_exception_id text, expected_exception_version integer,
 lease_token text, lease_until timestamptz, bytes_received bigint NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 UNIQUE(tenant_id,owner_id,idempotency_key),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_item (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, operation_id text NOT NULL,
 position integer NOT NULL, file_ref jsonb NOT NULL, state text NOT NULL DEFAULT 'pending',
 receipt_id text, object_key text, actual_sha256 text, content_type text, filename text,
 size_bytes bigint, invoice_id text, exception_id text, error_code text,
 PRIMARY KEY(tenant_id,legal_entity_id,operation_id,position),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id) REFERENCES finance_operation);
CREATE TABLE IF NOT EXISTS finance_invoice (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 owner_id text NOT NULL, seller_tax_id text NOT NULL, number text NOT NULL,
 issue_date text NOT NULL, data jsonb NOT NULL, canonical_digest text NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 UNIQUE(tenant_id,legal_entity_id,seller_tax_id,number,issue_date),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_invoice_line (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, invoice_id text NOT NULL,
 position integer NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,invoice_id,position),
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice);
CREATE TABLE IF NOT EXISTS finance_invoice_field (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, invoice_id text NOT NULL,
 position integer NOT NULL, data jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,invoice_id,position),
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice);
CREATE TABLE IF NOT EXISTS finance_receipt (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL,
 owner_id text NOT NULL, operation_id text NOT NULL, position integer NOT NULL,
 object_key text NOT NULL, sha256 text NOT NULL, file_ref jsonb NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id,position) REFERENCES finance_item,
 UNIQUE(tenant_id,legal_entity_id,operation_id,position));
CREATE TABLE IF NOT EXISTS finance_invoice_receipt (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, invoice_id text NOT NULL, receipt_id text NOT NULL,
 PRIMARY KEY(tenant_id,legal_entity_id,invoice_id,receipt_id),
 FOREIGN KEY(tenant_id,legal_entity_id,invoice_id) REFERENCES finance_invoice,
 FOREIGN KEY(tenant_id,legal_entity_id,receipt_id) REFERENCES finance_receipt);
CREATE TABLE IF NOT EXISTS finance_exception (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL, owner_id text NOT NULL,
 operation_id text NOT NULL, item_position integer NOT NULL,
 version integer NOT NULL DEFAULT 1, rule_id text NOT NULL, status text NOT NULL DEFAULT 'open',
 receipt_id text, resolved_invoice_id text, resolved_operation_id text,
 received_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(tenant_id,legal_entity_id,id), UNIQUE(id),
 FOREIGN KEY(tenant_id,legal_entity_id,operation_id,item_position) REFERENCES finance_item);
CREATE TABLE IF NOT EXISTS finance_idempotency (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, owner_id text NOT NULL, key text NOT NULL, request_hash text NOT NULL,
 response jsonb NOT NULL, PRIMARY KEY(tenant_id,owner_id,key),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
CREATE TABLE IF NOT EXISTS finance_event (
 sequence bigserial PRIMARY KEY, tenant_id text NOT NULL, legal_entity_id text NOT NULL,
 aggregate_id text NOT NULL, kind text NOT NULL, data jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS finance_context_replay (
 issuer text NOT NULL, jti text NOT NULL, subject text NOT NULL,
 request_hash text NOT NULL, idempotency_key text, expires_at timestamptz NOT NULL,
 PRIMARY KEY(issuer,jti));
ALTER TABLE finance_company ADD COLUMN IF NOT EXISTS received_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE finance_invoice ADD COLUMN IF NOT EXISTS received_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE finance_exception ADD COLUMN IF NOT EXISTS received_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS finance_company_page ON finance_company(tenant_id,received_at,legal_entity_id);
CREATE INDEX IF NOT EXISTS finance_invoice_page ON finance_invoice(tenant_id,legal_entity_id,received_at,id);
CREATE INDEX IF NOT EXISTS finance_exception_page ON finance_exception(tenant_id,legal_entity_id,received_at,id);
CREATE INDEX IF NOT EXISTS finance_pending ON finance_operation(status,lease_until,created_at);
CREATE INDEX IF NOT EXISTS finance_replay_expiry ON finance_context_replay(expires_at);
"""
