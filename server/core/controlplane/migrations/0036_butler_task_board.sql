-- 管家跨轮持久任务板（ADR 0024）：按 (tenant_id, user_id) 隔离，支持 DAG 步骤依赖与实时焦点。
CREATE TABLE "${schema}"."${prefix}butler_task_board" (
 tenant_id TEXT NOT NULL,
 board_id TEXT NOT NULL,
 user_id TEXT COLLATE "C" NOT NULL,
 trace_id TEXT NOT NULL,
 active BOOLEAN NOT NULL,
 updated_at TIMESTAMPTZ NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id, board_id),
 UNIQUE(tenant_id, user_id, trace_id),
 FOREIGN KEY(tenant_id, user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id, user_id)
);
CREATE INDEX "${prefix}butler_task_board_active" ON "${schema}"."${prefix}butler_task_board"
 (tenant_id, user_id, updated_at DESC, board_id) WHERE active;
