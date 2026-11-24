-- P1 用户身份：平台用户、外部身份、租户身份配置、登录会话、登录尝试、按用户用量、对账状态；
-- 已有的会话表、审计表加 user_id，凭据表加 can_act_as_user。旧行的新列取默认值（空或 FALSE）。
--
-- 参与排序与翻页的文本键（用户标识、外部标识）声明为 COLLATE "C"：按码点比较，与内存实现的
-- 字符串比较一致，不受库默认排序规则影响，查询也就不必再写 COLLATE，主键索引能直接用上。

-- 会话属于哪名员工；API key 直接建的为空。成员只看自己的会话，按它列出。
ALTER TABLE "${schema}"."${prefix}session" ADD COLUMN user_id TEXT;
CREATE INDEX "${prefix}session_user_idx"
    ON "${schema}"."${prefix}session" (tenant_id, user_id, updated_at DESC, session_id DESC)
    WHERE user_id IS NOT NULL;

-- 审计记录关联的员工；按员工查审计。
ALTER TABLE "${schema}"."${prefix}audit" ADD COLUMN user_id TEXT;
CREATE INDEX "${prefix}audit_user_idx"
    ON "${schema}"."${prefix}audit" (tenant_id, user_id, audit_id)
    WHERE user_id IS NOT NULL;

-- 能否代员工调用；要在签发时显式授予。
ALTER TABLE "${schema}"."${prefix}api_key"
    ADD COLUMN can_act_as_user BOOLEAN NOT NULL DEFAULT FALSE;

-- 平台用户。不删行：停用只改状态。
CREATE TABLE "${schema}"."${prefix}user" (
    tenant_id        TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    user_id          TEXT COLLATE "C" NOT NULL,
    display_name     TEXT NOT NULL,
    created_at       DOUBLE PRECISION NOT NULL,
    role             TEXT NOT NULL,
    avatar_url       TEXT,
    status           TEXT NOT NULL,
    disabled_reason  TEXT,
    last_login_at    DOUBLE PRECISION,
    disabled_at      DOUBLE PRECISION,
    unverified_since DOUBLE PRECISION,
    PRIMARY KEY (tenant_id, user_id)
);

-- 外部身份：飞书的 union_id 等。同一租户里，同一身份源的同一标识只对应一个用户，一个用户在
-- 同一身份源里也只有一个外部身份。
CREATE TABLE "${schema}"."${prefix}external_identity" (
    tenant_id  TEXT NOT NULL,
    provider   TEXT NOT NULL,
    subject    TEXT COLLATE "C" NOT NULL,
    user_id    TEXT COLLATE "C" NOT NULL,
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, provider, subject),
    CONSTRAINT "${prefix}external_identity_user_key" UNIQUE (tenant_id, provider, user_id),
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);

-- 租户接入身份源的配置。三个密钥只存保管库里的名字。
CREATE TABLE "${schema}"."${prefix}identity_config" (
    tenant_id              TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    provider               TEXT NOT NULL,
    app_id                 TEXT NOT NULL,
    app_secret_ref         TEXT NOT NULL,
    encrypt_key_ref        TEXT NOT NULL,
    verification_token_ref TEXT NOT NULL,
    feishu_tenant_key      TEXT,
    session_ttl_seconds    INTEGER,
    PRIMARY KEY (tenant_id, provider)
);

-- 浏览器登录会话。只存令牌的摘要；请求只带 cookie，按摘要查。过期的由维护任务按 expires_at 清理。
CREATE TABLE "${schema}"."${prefix}login_session" (
    token_hash   TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    user_id      TEXT COLLATE "C" NOT NULL,
    created_at   DOUBLE PRECISION NOT NULL,
    expires_at   DOUBLE PRECISION NOT NULL,
    last_seen_at DOUBLE PRECISION NOT NULL,
    FOREIGN KEY (tenant_id, user_id)
        REFERENCES "${schema}"."${prefix}user" (tenant_id, user_id)
);
CREATE INDEX "${prefix}login_session_user_idx"
    ON "${schema}"."${prefix}login_session" (tenant_id, user_id);
CREATE INDEX "${prefix}login_session_expiry_idx"
    ON "${schema}"."${prefix}login_session" (expires_at);

-- 进行中的登录（授权页往返）。取出即删；放在库里是为了多个实例都能认。没走完的由维护任务清理。
-- cookie_hash 是登录尝试 cookie 的摘要：回调时浏览器出示的 cookie 必须与它对上（防登录伪造）。
CREATE TABLE "${schema}"."${prefix}login_attempt" (
    state         TEXT PRIMARY KEY,
    tenant_id     TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    code_verifier TEXT NOT NULL,
    return_to     TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    expires_at    DOUBLE PRECISION NOT NULL,
    cookie_hash   TEXT NOT NULL
);
CREATE INDEX "${prefix}login_attempt_expiry_idx"
    ON "${schema}"."${prefix}login_attempt" (expires_at);

-- 按用户的用量；租户总量那张表照旧。
CREATE TABLE "${schema}"."${prefix}user_usage" (
    tenant_id     TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    day           TEXT NOT NULL,
    user_id       TEXT COLLATE "C" NOT NULL,
    input_tokens  BIGINT NOT NULL DEFAULT 0,
    output_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens  BIGINT NOT NULL DEFAULT 0,
    turns         BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, day, user_id)
);

-- 每个租户一条对账状态：多个实例每个周期只对一次。
CREATE TABLE "${schema}"."${prefix}identity_reconcile" (
    tenant_id        TEXT PRIMARY KEY REFERENCES "${schema}"."${prefix}tenant" (tenant_id),
    last_run_at      DOUBLE PRECISION,
    lease_owner      TEXT,
    lease_expires_at DOUBLE PRECISION NOT NULL DEFAULT 0
);
