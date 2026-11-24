-- 会话：业务方附加的关联标识。
ALTER TABLE "${schema}"."${prefix}session" ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX "${prefix}session_creator_idx"
    ON "${schema}"."${prefix}session" (tenant_id, created_by, updated_at DESC);
