-- R4-D1 知识问答上下文：固定命中集与回答缓存，属于员工本人；来源失效即标 blocked 并清掉回答正文。
CREATE TABLE "${schema}"."${prefix}knowledge_context" (
 tenant_id TEXT NOT NULL,owner TEXT COLLATE "C" NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,owner) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}knowledge_context_by_owner" ON "${schema}"."${prefix}knowledge_context"(tenant_id,owner);
