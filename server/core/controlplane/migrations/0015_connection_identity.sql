-- 员工连接与内部凭据不进入普通租户密钥API。
CREATE TABLE ${schema}.${prefix}connection_record (
    tenant_id TEXT NOT NULL REFERENCES ${schema}.${prefix}tenant(tenant_id),
    id TEXT NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (tenant_id,id)
);
CREATE TABLE ${schema}.${prefix}connection_credential (
    tenant_id TEXT NOT NULL REFERENCES ${schema}.${prefix}tenant(tenant_id),
    id TEXT NOT NULL,
    ciphertext BYTEA NOT NULL,
    PRIMARY KEY (tenant_id,id)
);
-- 仅OAuth随机state摘要作为全局索引；回调由持久记录反查租户，不接受参数覆盖。
CREATE TABLE ${schema}.${prefix}connection_auth_attempt (
    state_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES ${schema}.${prefix}tenant(tenant_id),
    expires_at DOUBLE PRECISION NOT NULL,
    record JSONB NOT NULL
);
CREATE INDEX ON ${schema}.${prefix}connection_auth_attempt(expires_at);
