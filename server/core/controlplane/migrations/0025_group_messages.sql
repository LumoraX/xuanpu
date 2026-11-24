-- 会诊消息区（R5-D2）：只追加。成员发布的结论与追问回复、管家的收口说明；按 (编组, 键) 去重。
CREATE TABLE "${schema}"."${prefix}butler_group_message" (
 tenant_id TEXT NOT NULL,group_id TEXT NOT NULL,seq INTEGER NOT NULL,user_id TEXT COLLATE "C" NOT NULL,
 msg_key TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,group_id,seq),UNIQUE(tenant_id,group_id,msg_key),
 FOREIGN KEY(tenant_id,group_id) REFERENCES "${schema}"."${prefix}butler_group"(tenant_id,group_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
