-- 任务、输入固定引用、状态事件与员工工作区共用同一身份事务。
CREATE TABLE "${schema}"."${prefix}work_task" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    task_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    input_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('accepted','running','waiting_user',
        'succeeded','failed','cancelled','outcome_unknown','interrupted')),
    state_version BIGINT NOT NULL CHECK (state_version > 0),
    process_owner TEXT NOT NULL,
    accepted_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    finished_at DOUBLE PRECISION,
    record JSONB NOT NULL,
    PRIMARY KEY (tenant_id,user_id,task_id),
    UNIQUE (tenant_id,user_id,request_id),
    FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
CREATE INDEX "${prefix}work_task_recent" ON "${schema}"."${prefix}work_task"
    (tenant_id,user_id,accepted_at DESC,task_id);
CREATE TABLE "${schema}"."${prefix}work_task_input" (
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    task_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    record JSONB NOT NULL,
    PRIMARY KEY (tenant_id,user_id,task_id,item_id,revision),
    FOREIGN KEY (tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task" (tenant_id,user_id,task_id),
    FOREIGN KEY (tenant_id,user_id,item_id,revision) REFERENCES "${schema}"."${prefix}workspace_file_version" (tenant_id,user_id,item_id,revision)
);
CREATE TABLE "${schema}"."${prefix}work_task_event" (
    event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT COLLATE "C" NOT NULL,
    task_id TEXT NOT NULL,
    state_version BIGINT NOT NULL,
    at DOUBLE PRECISION NOT NULL,
    event_kind TEXT NOT NULL,
    data JSONB NOT NULL,
    FOREIGN KEY (tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task" (tenant_id,user_id,task_id),
    UNIQUE (tenant_id,user_id,task_id,state_version)
);
CREATE INDEX "${prefix}work_task_event_cursor" ON "${schema}"."${prefix}work_task_event"
    (tenant_id,user_id,task_id,event_id);
ALTER TABLE "${schema}"."${prefix}session" ADD COLUMN work_task_id TEXT;
ALTER TABLE "${schema}"."${prefix}session" ADD CONSTRAINT "${prefix}session_work_task_fk"
    FOREIGN KEY (tenant_id,user_id,work_task_id)
    REFERENCES "${schema}"."${prefix}work_task" (tenant_id,user_id,task_id);

ALTER TABLE "${schema}"."${prefix}session" ADD COLUMN contains_external_input BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE "${schema}"."${prefix}session" SET contains_external_input = TRUE WHERE user_id IS NOT NULL;
