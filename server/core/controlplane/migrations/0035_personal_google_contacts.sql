-- 纯个人空间 Google 通讯录关联：OAuth 一次性尝试、密文凭据与增量同步游标、Google resourceName 映射。
CREATE TABLE "${schema}"."${prefix}personal_google_attempt" (
    state_hash TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    cookie_hash TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    return_to TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

CREATE INDEX "${prefix}personal_google_attempt_exp_idx"
    ON "${schema}"."${prefix}personal_google_attempt" (expires_at);

CREATE TABLE "${schema}"."${prefix}personal_google_account" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    google_sub TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '',
    token_revision TEXT NOT NULL,
    access_token_cipher BYTEA NOT NULL,
    refresh_token_cipher BYTEA,
    access_expires_at DOUBLE PRECISION NOT NULL,
    sync_token TEXT,
    linked_at DOUBLE PRECISION NOT NULL,
    last_synced_at DOUBLE PRECISION,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

CREATE TABLE "${schema}"."${prefix}personal_google_contact_link" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    resource_name TEXT NOT NULL,
    person_id TEXT NOT NULL,
    etag TEXT NOT NULL DEFAULT '',
    synced_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id, user_id, resource_name),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

CREATE INDEX "${prefix}personal_google_contact_person_idx"
    ON "${schema}"."${prefix}personal_google_contact_link" (tenant_id, user_id, person_id);
