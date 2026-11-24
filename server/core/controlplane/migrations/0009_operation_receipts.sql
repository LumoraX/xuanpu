-- 审批认领与回执不依赖异步审计视图；已发布迁移只增不改。
CREATE TABLE "${schema}"."${prefix}operation_receipt" (
    tenant_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('claimed','submitted','applied','rejected','cancelled','outcome_unknown')),
    submission_id TEXT,
    created_at DOUBLE PRECISION NOT NULL,
    updated_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (tenant_id,session_id,request_id),
    FOREIGN KEY (tenant_id,session_id) REFERENCES "${schema}"."${prefix}session"(tenant_id,session_id)
);
CREATE INDEX "${prefix}operation_receipt_operation_idx"
    ON "${schema}"."${prefix}operation_receipt"(tenant_id,operation_id);
