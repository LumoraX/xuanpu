-- 主动业务与统一通知：全部外键限定租户、员工，复用员工行锁和通知游标。
CREATE TABLE "${schema}"."${prefix}proactive_check" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    check_id TEXT NOT NULL,
    record JSONB NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    item_id TEXT GENERATED ALWAYS AS (record->>'item_id') STORED NOT NULL,
    generation INTEGER GENERATED ALWAYS AS ((record->>'generation')::integer) STORED NOT NULL,
    check_type TEXT GENERATED ALWAYS AS (record->>'check_type') STORED NOT NULL,
    stage_index INTEGER GENERATED ALWAYS AS ((record->>'stage_index')::integer) STORED NOT NULL,
    PRIMARY KEY (tenant_id,user_id,check_id),
    UNIQUE (tenant_id,user_id,item_id,generation,check_type,stage_index),
    CHECK (generation > 0 AND stage_index >= 0 AND check_type IN ('overdue','stale')),
    FOREIGN KEY (tenant_id,user_id,item_id) REFERENCES "${schema}"."${prefix}workspace_item" (tenant_id,user_id,item_id)
);
CREATE INDEX "${prefix}proactive_check_status_idx" ON "${schema}"."${prefix}proactive_check"
    (tenant_id,user_id,(record->>'status'));
CREATE TABLE "${schema}"."${prefix}proactive_followup" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    followup_id TEXT NOT NULL,
    record JSONB NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    item_id TEXT GENERATED ALWAYS AS (record->>'item_id') STORED,
    PRIMARY KEY (tenant_id,user_id,followup_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id),
    FOREIGN KEY (tenant_id,user_id,item_id) REFERENCES "${schema}"."${prefix}workspace_item" (tenant_id,user_id,item_id)
);
CREATE UNIQUE INDEX "${prefix}proactive_followup_stable_idx" ON "${schema}"."${prefix}proactive_followup"
    (tenant_id,user_id,(record->>'stable_key')) WHERE record->>'stable_key' IS NOT NULL;
CREATE INDEX "${prefix}proactive_followup_review_idx" ON "${schema}"."${prefix}proactive_followup"
    (tenant_id,user_id,(record->>'next_review_at'));
CREATE TABLE "${schema}"."${prefix}proactive_scan_state" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    record JSONB NOT NULL CHECK (jsonb_typeof(record) = 'object'),
    PRIMARY KEY (tenant_id,user_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
-- 旧记录只以创建时间回填，不能把未知进展解释为已核实停滞。
UPDATE "${schema}"."${prefix}workspace_item"
SET record = record || jsonb_build_object('generation',1,'last_progress_at',record->'created_at','progress_unknown',true)
WHERE kind='todo' AND NOT record ? 'generation';
ALTER TABLE "${schema}"."${prefix}reminder" ADD UNIQUE (tenant_id,user_id,reminder_id);
ALTER TABLE "${schema}"."${prefix}notification"
    ALTER COLUMN reminder_id DROP NOT NULL,
    ALTER COLUMN due_at DROP NOT NULL,
    ADD COLUMN notification_key TEXT,
    ADD COLUMN source TEXT NOT NULL DEFAULT 'reminder',
    ADD COLUMN body TEXT CHECK (length(body) <= 4000),
    ADD COLUMN proactive_check_id TEXT,
    ADD COLUMN proactive_followup_id TEXT,
    ADD COLUMN alert_after DOUBLE PRECISION NOT NULL DEFAULT 0,
    ADD UNIQUE (tenant_id,user_id,notification_key),
    ADD UNIQUE (tenant_id,user_id,notification_id),
    ADD CHECK (
        (source='reminder' AND reminder_id IS NOT NULL AND due_at IS NOT NULL
            AND proactive_check_id IS NULL AND proactive_followup_id IS NULL)
        OR (source='proactive_check' AND reminder_id IS NULL AND proactive_check_id IS NOT NULL
            AND proactive_followup_id IS NULL AND notification_key IS NOT NULL)
        OR (source='proactive_followup' AND reminder_id IS NULL AND proactive_followup_id IS NOT NULL
            AND proactive_check_id IS NULL AND notification_key IS NOT NULL)
    ),
    ADD FOREIGN KEY (tenant_id,user_id,reminder_id) REFERENCES "${schema}"."${prefix}reminder" (tenant_id,user_id,reminder_id),
    ADD FOREIGN KEY (tenant_id,user_id,proactive_check_id) REFERENCES "${schema}"."${prefix}proactive_check" (tenant_id,user_id,check_id),
    ADD FOREIGN KEY (tenant_id,user_id,proactive_followup_id) REFERENCES "${schema}"."${prefix}proactive_followup" (tenant_id,user_id,followup_id);
UPDATE "${schema}"."${prefix}notification" SET alert_after=created_at;
ALTER TABLE "${schema}"."${prefix}notification_delivery"
    ADD FOREIGN KEY (tenant_id,user_id,notification_id) REFERENCES "${schema}"."${prefix}notification" (tenant_id,user_id,notification_id);

-- 规则检查本身即唯一来源，换一个调用方去重键也不能重复催办。
CREATE UNIQUE INDEX "${prefix}notification_check_source_idx" ON "${schema}"."${prefix}notification"
    (tenant_id,user_id,proactive_check_id) WHERE proactive_check_id IS NOT NULL;

-- 本人员工业务会话的稳定扫描，避免最近活动排序造成历史会话饥饿。
CREATE INDEX "${prefix}session_user_scan_idx" ON "${schema}"."${prefix}session"
    (tenant_id,user_id,session_id COLLATE "C");
