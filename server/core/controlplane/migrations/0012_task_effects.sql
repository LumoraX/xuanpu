-- 意图先于派发，回执保留原调用标识和参数摘要。
CREATE TABLE "${schema}"."${prefix}task_effect" (
 tenant_id TEXT NOT NULL, user_id TEXT COLLATE "C" NOT NULL,
 logical_action_id TEXT NOT NULL, task_id TEXT NOT NULL, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,user_id,logical_action_id),
 FOREIGN KEY(tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task"(tenant_id,user_id,task_id)
);
CREATE INDEX "${prefix}task_effect_task" ON "${schema}"."${prefix}task_effect"(tenant_id,user_id,task_id);
