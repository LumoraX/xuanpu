-- 组织域与跨员工协调事务；所有外键含租户，回执与事件同连接提交。
ALTER TABLE "${schema}"."${prefix}user"
 ADD COLUMN lifecycle_epoch BIGINT NOT NULL DEFAULT 1 CHECK(lifecycle_epoch>=1),
 ADD COLUMN profile_version BIGINT NOT NULL DEFAULT 1 CHECK(profile_version>=1);
CREATE TABLE "${schema}"."${prefix}organization_unit" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id),
 id TEXT NOT NULL, parent_id TEXT, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,parent_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}organization_position" (
 tenant_id TEXT NOT NULL, id TEXT NOT NULL, unit_id TEXT NOT NULL, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,unit_id,id),
 FOREIGN KEY(tenant_id,unit_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}organization_membership" (
 tenant_id TEXT NOT NULL, id TEXT NOT NULL, user_id TEXT NOT NULL, unit_id TEXT NOT NULL, position_id TEXT, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id),
 FOREIGN KEY(tenant_id,unit_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id),
 FOREIGN KEY(tenant_id,unit_id,position_id) REFERENCES "${schema}"."${prefix}organization_position"(tenant_id,unit_id,id)
);
CREATE TABLE "${schema}"."${prefix}organization_role" (
 tenant_id TEXT NOT NULL, id TEXT NOT NULL, user_id TEXT NOT NULL, scope_id TEXT,
 role TEXT NOT NULL CHECK(role IN ('org_manager','unit_operator','unit_approver')), record JSONB NOT NULL,
 CHECK(scope_id IS NOT NULL OR role='org_manager'),
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id),
 FOREIGN KEY(tenant_id,scope_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}organization_authority" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id),
 id TEXT NOT NULL, object_type TEXT NOT NULL, field TEXT NOT NULL, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,object_type,field)
);
CREATE TABLE "${schema}"."${prefix}organization_mapping" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id),
 id TEXT NOT NULL, source TEXT NOT NULL, object_type TEXT NOT NULL, external_id TEXT NOT NULL, local_id TEXT NOT NULL, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,source,object_type,external_id),
 UNIQUE(tenant_id,source,object_type,local_id)
);
CREATE TABLE "${schema}"."${prefix}organization_authority_preview" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id),
 id TEXT NOT NULL, record JSONB NOT NULL, PRIMARY KEY(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}organization_preview" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id), id TEXT NOT NULL,
 record JSONB NOT NULL, PRIMARY KEY(tenant_id,id)
);
CREATE TABLE "${schema}"."${prefix}coordination_receipt" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id),
 actor TEXT NOT NULL, request_id TEXT NOT NULL, record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,actor,request_id)
);
CREATE TABLE "${schema}"."${prefix}coordination_outbox" (
 tenant_id TEXT NOT NULL REFERENCES "${schema}"."${prefix}tenant"(tenant_id),
 id TEXT NOT NULL, kind TEXT NOT NULL, entity_id TEXT NOT NULL, entity_version BIGINT NOT NULL,
 record JSONB NOT NULL, PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,kind,entity_id,entity_version)
);
CREATE TABLE "${schema}"."${prefix}coordination_ack" (
 tenant_id TEXT NOT NULL, consumer TEXT NOT NULL, event_id TEXT NOT NULL,
 PRIMARY KEY(tenant_id,consumer,event_id),
 FOREIGN KEY(tenant_id,event_id) REFERENCES "${schema}"."${prefix}coordination_outbox"(tenant_id,id)
);
