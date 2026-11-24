-- 管家派工（R5-C）：一次派工一行。只存任务说明与材料引用；匠人的正文与结果在其任务或会话里。
CREATE TABLE "${schema}"."${prefix}butler_dispatch" (
 tenant_id TEXT NOT NULL,dispatch_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,
 request_id TEXT NOT NULL,trace_id TEXT NOT NULL,active BOOLEAN NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,dispatch_id),UNIQUE(tenant_id,user_id,request_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}butler_dispatch_trace" ON "${schema}"."${prefix}butler_dispatch"
 (tenant_id,user_id,trace_id,created_at,dispatch_id);
CREATE INDEX "${prefix}butler_dispatch_active" ON "${schema}"."${prefix}butler_dispatch"
 (tenant_id,trace_id) WHERE active;
