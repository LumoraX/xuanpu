-- 过程产物（Trace Artifact）全局索引（ADR 0025 / ADR 0026）：跨节点只读接力与正式工作区分层。
CREATE TABLE "${schema}"."${prefix}trace_artifact" (
 tenant_id TEXT NOT NULL,
 artifact_id TEXT NOT NULL,
 user_id TEXT COLLATE "C" NOT NULL,
 trace_id TEXT NOT NULL,
 dispatch_id TEXT,
 created_at TIMESTAMPTZ NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id, artifact_id),
 FOREIGN KEY(tenant_id, user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id, user_id)
);
CREATE INDEX "${prefix}trace_artifact_trace" ON "${schema}"."${prefix}trace_artifact"
 (tenant_id, user_id, trace_id, created_at, artifact_id);
