-- 跨节点本地 Transcript 全局元数据索引（ADR 0026）：仅存元数据指针，不存大正文。
CREATE TABLE "${schema}"."${prefix}transcript_index" (
 tenant_id TEXT NOT NULL,
 span_id TEXT NOT NULL,
 trace_id TEXT NOT NULL,
 user_id TEXT COLLATE "C",
 kind TEXT NOT NULL,
 node_id TEXT NOT NULL,
 status TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id, span_id)
);
CREATE INDEX "${prefix}transcript_index_trace" ON "${schema}"."${prefix}transcript_index"
 (tenant_id, trace_id, created_at, span_id);
CREATE INDEX "${prefix}transcript_index_status" ON "${schema}"."${prefix}transcript_index"
 (tenant_id, status, created_at DESC);
