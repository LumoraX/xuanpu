-- 微内核插件与管家扩展状态的统一零 DDL 持久化集合（ADR 0027）：
-- 支持多节点共享、按 (tenant_id, plugin_id, user_id, collection, record_id) 强隔离与乐观锁版本控制。
CREATE TABLE "${schema}"."${prefix}plugin_collection" (
 tenant_id TEXT NOT NULL,
 plugin_id TEXT NOT NULL,
 user_id TEXT NOT NULL,
 collection TEXT NOT NULL,
 record_id TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 data JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at DOUBLE PRECISION NOT NULL,
 updated_at DOUBLE PRECISION NOT NULL,
 PRIMARY KEY(tenant_id, plugin_id, user_id, collection, record_id)
);
CREATE INDEX "${prefix}plugin_collection_lookup"
 ON "${schema}"."${prefix}plugin_collection"
 (tenant_id, plugin_id, user_id, collection, updated_at DESC);
