-- xuanpu 控制面的表。${schema} / ${prefix} 由迁移工具代入，代入前已按标识符字符集校验。
-- 除租户表与凭据表外，每张表都带 tenant_id，所有查询都按它过滤。

CREATE TABLE "${schema}"."${prefix}tenant" (
    tenant_id           TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    status              TEXT NOT NULL,
    max_active_sessions INTEGER,
    max_daily_tokens    BIGINT,
    created_at          DOUBLE PRECISION NOT NULL
);

CREATE TABLE "${schema}"."${prefix}api_key" (
    key_id      TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    revoked_at  DOUBLE PRECISION
);
CREATE INDEX "${prefix}api_key_tenant_idx" ON "${schema}"."${prefix}api_key" (tenant_id);

CREATE TABLE "${schema}"."${prefix}package_version" (
    tenant_id     TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    digest        TEXT NOT NULL,
    manifest_yaml TEXT NOT NULL,
    location      TEXT NOT NULL,
    published_at  DOUBLE PRECISION NOT NULL,
    published_by  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name, version)
);

CREATE TABLE "${schema}"."${prefix}package_active" (
    tenant_id    TEXT NOT NULL,
    name         TEXT NOT NULL,
    version      TEXT NOT NULL,
    activated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, name),
    FOREIGN KEY (tenant_id, name, version)
        REFERENCES "${schema}"."${prefix}package_version" (tenant_id, name, version)
);

CREATE TABLE "${schema}"."${prefix}secret" (
    tenant_id  TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    name       TEXT NOT NULL,
    ciphertext BYTEA NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name)
);

CREATE TABLE "${schema}"."${prefix}session" (
    tenant_id        TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    session_id       TEXT NOT NULL,
    package          TEXT NOT NULL,
    version          TEXT NOT NULL,
    entry            TEXT NOT NULL,
    thread_id        TEXT,
    status           TEXT NOT NULL,
    owner            TEXT,
    lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at       DOUBLE PRECISION NOT NULL,
    updated_at       DOUBLE PRECISION NOT NULL,
    created_by       TEXT NOT NULL,
    PRIMARY KEY (tenant_id, session_id)
);
CREATE INDEX "${prefix}session_recent_idx"
    ON "${schema}"."${prefix}session" (tenant_id, updated_at DESC, session_id DESC);
CREATE INDEX "${prefix}session_active_idx"
    ON "${schema}"."${prefix}session" (tenant_id) WHERE status = 'active';

CREATE TABLE "${schema}"."${prefix}usage" (
    tenant_id     TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    day           TEXT NOT NULL,
    input_tokens  BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens  BIGINT NOT NULL DEFAULT 0,
    turns         BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day)
);

-- 审计表只追加。不给应用账号 UPDATE / DELETE 权限是部署侧的事，这里不建任何更新路径。
CREATE TABLE "${schema}"."${prefix}audit" (
    audit_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    source     TEXT NOT NULL,
    kind       TEXT NOT NULL,
    at         DOUBLE PRECISION NOT NULL,
    actor      TEXT NOT NULL,
    session_id TEXT,
    stream     TEXT,
    seq        BIGINT,
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- 引擎事件按 (stream, seq) 去重；其余来源这两列为空，空值互不相等，不会被去重
    CONSTRAINT "${prefix}audit_stream_key" UNIQUE (tenant_id, stream, seq)
);
CREATE INDEX "${prefix}audit_tenant_idx" ON "${schema}"."${prefix}audit" (tenant_id, audit_id);
CREATE INDEX "${prefix}audit_session_idx"
    ON "${schema}"."${prefix}audit" (tenant_id, session_id, audit_id);
