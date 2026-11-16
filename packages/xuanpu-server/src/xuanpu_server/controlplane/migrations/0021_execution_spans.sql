-- 执行追踪（R5-B）：一次请求一条 trace，每个执行单元一个 span。只存元数据，不存正文、参数与输出。
CREATE TABLE "${schema}"."${prefix}trace" (
 tenant_id TEXT NOT NULL,trace_id TEXT NOT NULL,user_id TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,active BOOLEAN NOT NULL DEFAULT FALSE,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,trace_id),UNIQUE(trace_id),
 FOREIGN KEY(tenant_id) REFERENCES "${schema}"."${prefix}tenant"(tenant_id)
);
CREATE INDEX "${prefix}trace_owner_created" ON "${schema}"."${prefix}trace"
 (tenant_id,user_id,created_at DESC,trace_id DESC);
CREATE TABLE "${schema}"."${prefix}trace_span" (
 tenant_id TEXT NOT NULL,trace_id TEXT NOT NULL,span_id TEXT NOT NULL,
 started_at TIMESTAMPTZ NOT NULL,terminal BOOLEAN NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,trace_id,span_id),
 FOREIGN KEY(tenant_id,trace_id) REFERENCES "${schema}"."${prefix}trace"(tenant_id,trace_id) ON DELETE CASCADE
);
