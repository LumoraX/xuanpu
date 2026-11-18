-- R4-D 本人知识来源与文本块：来源属于员工本人，文本块是派生数据，删除来源同时清掉文本块。
CREATE TABLE "${schema}"."${prefix}knowledge_source" (
 tenant_id TEXT NOT NULL,owner TEXT COLLATE "C" NOT NULL,id TEXT NOT NULL,ref TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}knowledge_source_by_owner" ON "${schema}"."${prefix}knowledge_source"(tenant_id,owner);
CREATE TABLE "${schema}"."${prefix}knowledge_chunk" (
 tenant_id TEXT NOT NULL,owner TEXT COLLATE "C" NOT NULL,source_id TEXT NOT NULL,ordinal INTEGER NOT NULL,text TEXT NOT NULL,
 PRIMARY KEY(tenant_id,source_id,ordinal),
 FOREIGN KEY(tenant_id,source_id) REFERENCES "${schema}"."${prefix}knowledge_source"(tenant_id,id)
);
-- 本人工作记忆：每条修订一行；只有本人确认才生效，更正生成新修订，删除不物理清除记录。
CREATE TABLE "${schema}"."${prefix}work_memory" (
 tenant_id TEXT NOT NULL,owner TEXT COLLATE "C" NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}work_memory_by_owner" ON "${schema}"."${prefix}work_memory"(tenant_id,owner);
