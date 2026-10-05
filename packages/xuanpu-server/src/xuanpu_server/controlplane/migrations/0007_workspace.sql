-- 原生个人工作台。用户行锁协调身份停用、配额和主对话序号。
CREATE TABLE "${schema}"."${prefix}workspace_settings" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    settings JSONB NOT NULL,
    next_message_id BIGINT NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id,user_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
CREATE TABLE "${schema}"."${prefix}workspace_item" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('folder','file','matter','todo','event','skill')),
    record JSONB NOT NULL,
    PRIMARY KEY (tenant_id,user_id,item_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
CREATE TABLE "${schema}"."${prefix}workspace_file_version" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    item_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    record JSONB NOT NULL,
    PRIMARY KEY (tenant_id,user_id,item_id,revision),
    FOREIGN KEY (tenant_id,user_id,item_id) REFERENCES "${schema}"."${prefix}workspace_item" (tenant_id,user_id,item_id)
);
CREATE TABLE "${schema}"."${prefix}workspace_request" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    request_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY (tenant_id,user_id,request_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
CREATE TABLE "${schema}"."${prefix}workspace_message" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    message_id BIGINT NOT NULL CHECK (message_id > 0),
    request_id TEXT NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (tenant_id,user_id,message_id),
    UNIQUE (tenant_id,user_id,request_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
-- 先过滤真正的助手建议，再反向读取最近待确认项。
CREATE INDEX "${prefix}workspace_pending_proposal_idx"
    ON "${schema}"."${prefix}workspace_message" (tenant_id,user_id,message_id DESC)
    WHERE record->>'role' = 'assistant'
      AND right(request_id,10) = ':assistant'
      AND (jsonb_typeof(record#>'{metadata,route,workspace_draft}') = 'object'
           OR jsonb_typeof(record#>'{metadata,route,reminder_draft}') = 'object');

-- 事项历史按本人事项过滤后分页，避免普通聊天占用窗口。
CREATE INDEX "${prefix}workspace_message_matter_idx"
    ON "${schema}"."${prefix}workspace_message" (tenant_id,user_id,(record->>'matter_id'),message_id);
