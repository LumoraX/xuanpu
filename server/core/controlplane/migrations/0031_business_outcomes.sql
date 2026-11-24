-- R4-F 验收批次与人工核实观察：都只追加；批次变更另建新批次，纠正观察另记一条。
CREATE TABLE "${schema}"."${prefix}acceptance_batch" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}acceptance_observation" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,batch_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,batch_id) REFERENCES "${schema}"."${prefix}acceptance_batch"(tenant_id,id)
);
