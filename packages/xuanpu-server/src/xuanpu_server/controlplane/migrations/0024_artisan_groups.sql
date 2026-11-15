-- 匠人编组（R5-D）：一个编组一行，计划与成员执行在 record 里（按版本整体保存）。
-- 成员各是一次派工（butler_dispatch），派工记录里带 group_id；匠人正文仍在其任务或会话里。
CREATE TABLE "${schema}"."${prefix}butler_group" (
 tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,
 request_id TEXT NOT NULL,trace_id TEXT NOT NULL,active BOOLEAN NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,group_id),UNIQUE(tenant_id,user_id,request_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}butler_group_active" ON "${schema}"."${prefix}butler_group"
 (tenant_id,created_at,group_id) WHERE active;
