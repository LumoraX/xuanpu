-- 三层不可变发布节点账本（L1 匠人 / L2 构造器 / L3 内核，ADR 0025）：只追加不物理删除，支持任意层安全回退。
CREATE TABLE "${schema}"."${prefix}release_node" (
 tenant_id TEXT NOT NULL,
 release_id TEXT NOT NULL,
 layer TEXT NOT NULL,
 target_name TEXT NOT NULL,
 generation INTEGER NOT NULL,
 state TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id, release_id),
 UNIQUE(tenant_id, layer, target_name, generation)
);
CREATE INDEX "${prefix}release_node_active" ON "${schema}"."${prefix}release_node"
 (tenant_id, layer, target_name, generation DESC);
