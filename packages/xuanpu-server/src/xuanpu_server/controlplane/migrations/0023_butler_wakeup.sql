-- 管家唤醒（R5-C2）：匠人一轮结束或需要本人决定时的唤醒事件，以及待本人处理项。
-- 只存派工引用、问题与答复文本（及出处）、处理决定；匠人正文仍在其任务或会话里。
CREATE TABLE "${schema}"."${prefix}butler_event" (
 tenant_id TEXT NOT NULL,event_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,
 dispatch_id TEXT NOT NULL,event_key TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,
 acked BOOLEAN NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,event_id),UNIQUE(tenant_id,dispatch_id,event_key),
 FOREIGN KEY(tenant_id,dispatch_id) REFERENCES "${schema}"."${prefix}butler_dispatch"(tenant_id,dispatch_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}butler_event_unacked" ON "${schema}"."${prefix}butler_event"
 (tenant_id,created_at,event_id) WHERE NOT acked;
CREATE TABLE "${schema}"."${prefix}butler_pending" (
 tenant_id TEXT NOT NULL,pending_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,
 dispatch_id TEXT NOT NULL,source_key TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,pending_id),UNIQUE(tenant_id,dispatch_id,source_key),
 FOREIGN KEY(tenant_id,dispatch_id) REFERENCES "${schema}"."${prefix}butler_dispatch"(tenant_id,dispatch_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}butler_pending_owner" ON "${schema}"."${prefix}butler_pending"
 (tenant_id,user_id,created_at,pending_id);
-- 对账器按租户扫进行中的派工
CREATE INDEX "${prefix}butler_dispatch_tenant_active" ON "${schema}"."${prefix}butler_dispatch"
 (tenant_id,created_at,dispatch_id) WHERE active;
