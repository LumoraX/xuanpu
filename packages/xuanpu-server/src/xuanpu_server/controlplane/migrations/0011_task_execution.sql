-- 独立执行授权以员工为边界，任务与派发在同一事务落库。
CREATE TABLE "${schema}"."${prefix}task_grant" (
 tenant_id TEXT NOT NULL, user_id TEXT COLLATE "C" NOT NULL, grant_id TEXT NOT NULL,
 record JSONB NOT NULL, PRIMARY KEY (tenant_id,user_id,grant_id),
 FOREIGN KEY (tenant_id,user_id) REFERENCES "${schema}"."${prefix}user" (tenant_id,user_id)
);
CREATE TABLE "${schema}"."${prefix}task_dispatch" (
 tenant_id TEXT NOT NULL, user_id TEXT COLLATE "C" NOT NULL, task_id TEXT NOT NULL,
 grant_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('ready','leased','retry_wait','blocked','waiting_user','paused_shutdown','settled')),
 due_at DOUBLE PRECISION NOT NULL, record JSONB NOT NULL,
 PRIMARY KEY (tenant_id,user_id,task_id),
 FOREIGN KEY (tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task" (tenant_id,user_id,task_id),
 FOREIGN KEY (tenant_id,user_id,grant_id) REFERENCES "${schema}"."${prefix}task_grant" (tenant_id,user_id,grant_id)
);
CREATE INDEX "${prefix}task_dispatch_due" ON "${schema}"."${prefix}task_dispatch" (state,due_at,tenant_id,user_id,task_id);
CREATE TABLE "${schema}"."${prefix}task_attempt" (
 tenant_id TEXT NOT NULL, user_id TEXT COLLATE "C" NOT NULL, attempt_key TEXT NOT NULL,
 task_id TEXT NOT NULL, attempt_no INTEGER NOT NULL CHECK(attempt_no>0), record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,user_id,attempt_key), UNIQUE(tenant_id,user_id,task_id,attempt_no),
 FOREIGN KEY (tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task" (tenant_id,user_id,task_id)
);
CREATE TABLE "${schema}"."${prefix}task_budget_reservation" (
 tenant_id TEXT NOT NULL, user_id TEXT COLLATE "C" NOT NULL, reservation_id TEXT NOT NULL,
 task_id TEXT NOT NULL, grant_id TEXT NOT NULL, day TEXT NOT NULL,
 charged BIGINT NOT NULL CHECK(charged>=0), record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,user_id,reservation_id),
 FOREIGN KEY (tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task" (tenant_id,user_id,task_id),
 FOREIGN KEY (tenant_id,user_id,grant_id) REFERENCES "${schema}"."${prefix}task_grant" (tenant_id,user_id,grant_id)
);
CREATE INDEX "${prefix}task_budget_day" ON "${schema}"."${prefix}task_budget_reservation" (tenant_id,day);
