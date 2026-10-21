"""原生财务通知派生索引及跨重启扫描进度。"""

from __future__ import annotations

F4_SCHEMA = """
CREATE TABLE IF NOT EXISTS finance_notification (
 tenant_id text NOT NULL, legal_entity_id text NOT NULL, id text NOT NULL UNIQUE,
 recipient_id text NOT NULL, source_type text NOT NULL, source_id text NOT NULL,
 source_revision text NOT NULL, source_status text NOT NULL,
 kind text NOT NULL, title text NOT NULL, target_type text NOT NULL,
 target_id text NOT NULL, version integer NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(), read_at timestamptz,
 PRIMARY KEY(tenant_id,legal_entity_id,id),
 UNIQUE(tenant_id,legal_entity_id,source_type,source_id,source_revision,recipient_id,kind),
 CHECK(target_type IN ('exception','expense_claim','payable','funding_case','action_plan','operation','red_letter_case','archive')),
 FOREIGN KEY(tenant_id,legal_entity_id) REFERENCES finance_company);
ALTER TABLE finance_notification DROP CONSTRAINT IF EXISTS finance_notification_target_type_check;
ALTER TABLE finance_notification ADD CONSTRAINT finance_notification_target_type_check CHECK(target_type IN ('exception','expense_claim','payable','funding_case','action_plan','operation','red_letter_case','archive'));
CREATE INDEX IF NOT EXISTS finance_notification_page ON finance_notification(tenant_id,legal_entity_id,recipient_id,created_at,id);
CREATE TABLE IF NOT EXISTS finance_notification_scan (
 singleton boolean PRIMARY KEY CHECK(singleton), after_key text NOT NULL);
INSERT INTO finance_notification_scan VALUES(true,'') ON CONFLICT DO NOTHING;
ALTER TABLE finance_outbox ADD COLUMN IF NOT EXISTS inbox_diagnostic text;
CREATE INDEX IF NOT EXISTS finance_outbox_native_pending ON finance_outbox(created_at,id) WHERE status='pending';
"""
