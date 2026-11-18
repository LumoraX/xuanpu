-- R4-C 隔离评测与按员工发布：证据与发布事实不物理删除；发布只能从生效改为停止或撤销。
CREATE TABLE "${schema}"."${prefix}capability_evaluation" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}capability_release" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id)
);
