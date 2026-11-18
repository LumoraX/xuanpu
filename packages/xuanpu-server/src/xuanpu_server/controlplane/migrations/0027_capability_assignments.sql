-- R4-B 到人能力分配：只授权使用能力，不授予资料、连接或审批权；不物理删除，撤销留痕。
CREATE TABLE "${schema}"."${prefix}capability_assignment" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,capability_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id)
);
CREATE INDEX "${prefix}capability_assignment_by_capability"
 ON "${schema}"."${prefix}capability_assignment"(tenant_id,capability_id);
-- 调用绑定：一次新受理（会话或任务）固定的能力依据，只写一次；续跑时对照当前资格复核。
CREATE TABLE "${schema}"."${prefix}capability_binding" (
 tenant_id TEXT NOT NULL,kind TEXT NOT NULL CHECK(kind IN ('session','task')),ref_id TEXT NOT NULL,
 user_id TEXT NOT NULL,capability_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,kind,ref_id),
 FOREIGN KEY(tenant_id,capability_id) REFERENCES "${schema}"."${prefix}capability_root"(tenant_id,id)
);
