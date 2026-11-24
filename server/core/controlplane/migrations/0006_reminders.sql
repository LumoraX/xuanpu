-- 玄圃持久提醒；员工锁内检查活动限额并分配通知游标。
CREATE TABLE "${schema}"."${prefix}reminder" (
    reminder_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 500),
    due_at DOUBLE PRECISION NOT NULL,
    timezone TEXT NOT NULL,
    repeat TEXT NOT NULL CHECK (repeat IN ('once', 'daily', 'weekly')),
    channels TEXT[] NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'completed', 'cancelled')),
    version INTEGER NOT NULL DEFAULT 1,
    request_id TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    original_fingerprint TEXT NOT NULL,
    UNIQUE (tenant_id, user_id, request_id),
    FOREIGN KEY (tenant_id, user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);
CREATE INDEX "${prefix}reminder_due_idx" ON "${schema}"."${prefix}reminder" (due_at, reminder_id) WHERE status = 'active';
CREATE INDEX "${prefix}reminder_owner_idx" ON "${schema}"."${prefix}reminder" (tenant_id, user_id, created_at DESC);
CREATE TABLE "${schema}"."${prefix}notification" (
    notification_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    reminder_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}reminder" (reminder_id),
    title TEXT NOT NULL,
    due_at DOUBLE PRECISION NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    read_at DOUBLE PRECISION,
    UNIQUE (reminder_id, due_at),
    FOREIGN KEY (tenant_id, user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);
CREATE INDEX "${prefix}notification_owner_idx" ON "${schema}"."${prefix}notification" (tenant_id, user_id, notification_id);
CREATE TABLE "${schema}"."${prefix}notification_delivery" (
    notification_id BIGINT NOT NULL REFERENCES "${schema}"."${prefix}notification" (notification_id),
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    channel TEXT NOT NULL CHECK (channel = 'feishu'),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','skipped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    claim_token TEXT,
    error_code TEXT,
    first_attempt_at DOUBLE PRECISION,
    PRIMARY KEY (notification_id, channel),
    FOREIGN KEY (tenant_id, user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);
CREATE INDEX "${prefix}delivery_due_idx" ON "${schema}"."${prefix}notification_delivery" (next_attempt_at) WHERE status IN ('pending','sending');
