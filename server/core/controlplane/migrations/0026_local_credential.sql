-- 玄圃自有账号（ADR 0019）：用户名口令凭据与一次性设置链接。
--
-- 用户名存规整后的小写形式，租户内唯一；口令只存 argon2id 摘要，为空表示已开通、尚未设口令。
-- 文本键声明为 COLLATE "C"，与身份域其余表一致。
CREATE TABLE "${schema}"."${prefix}local_credential" (
    tenant_id           TEXT NOT NULL,
    user_id             TEXT COLLATE "C" NOT NULL,
    username            TEXT COLLATE "C" NOT NULL,
    password_hash       TEXT,
    created_at          DOUBLE PRECISION NOT NULL,
    password_changed_at DOUBLE PRECISION,
    failed_attempts     INTEGER NOT NULL DEFAULT 0,
    locked_until        DOUBLE PRECISION,
    PRIMARY KEY (tenant_id, user_id),
    CONSTRAINT "${prefix}local_credential_username_key" UNIQUE (tenant_id, username),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

-- 一次性设置链接（开通设口令、重置口令）。只存令牌摘要；每名员工同时只有一条，新签发的替换旧的。
-- 兑现即删；过期未兑现的由身份域的周期清理删掉。
CREATE TABLE "${schema}"."${prefix}account_token" (
    token_hash TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT COLLATE "C" NOT NULL,
    purpose    TEXT NOT NULL,
    created_at DOUBLE PRECISION NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    issued_by  TEXT NOT NULL,
    CONSTRAINT "${prefix}account_token_user_key" UNIQUE (tenant_id, user_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);
CREATE INDEX "${prefix}account_token_expiry_idx"
    ON "${schema}"."${prefix}account_token" (expires_at);
