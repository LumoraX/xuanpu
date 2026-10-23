-- 共享领域采用同租户外键，固定材料、发布意图及来源依赖与协调事务一并提交。
CREATE TABLE "${schema}"."${prefix}shared_matter" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 owner_id TEXT NOT NULL,
 FOREIGN KEY(tenant_id,owner_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}shared_access_grant" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 matter_id TEXT NOT NULL,
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 user_id TEXT,
 unit_id TEXT,
 role_scope_id TEXT,
 FOREIGN KEY(tenant_id,matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,unit_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,role_scope_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(tenant_id,matter_id,id),
 CHECK((subject_kind='user' AND user_id=subject_id AND unit_id IS NULL AND user_id IS NOT NULL) OR (subject_kind='unit' AND unit_id=subject_id AND user_id IS NULL AND unit_id IS NOT NULL))
);
CREATE TABLE "${schema}"."${prefix}shared_member" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 matter_id TEXT NOT NULL,
 grant_id TEXT NOT NULL,
 subject_kind TEXT NOT NULL,
 subject_id TEXT NOT NULL,
 user_id TEXT,
 unit_id TEXT,
 FOREIGN KEY(tenant_id,matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,unit_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id,grant_id) REFERENCES "${schema}"."${prefix}shared_access_grant"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 CHECK((subject_kind='user' AND user_id=subject_id AND unit_id IS NULL AND user_id IS NOT NULL) OR (subject_kind='unit' AND unit_id=subject_id AND user_id IS NULL AND unit_id IS NOT NULL))
);
CREATE UNIQUE INDEX "${prefix}shared_unique_owner" ON "${schema}"."${prefix}shared_member"(tenant_id,matter_id) WHERE record->>'role'='owner' AND record->>'status'='active';
CREATE TABLE "${schema}"."${prefix}shared_event" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 matter_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 FOREIGN KEY(tenant_id,matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,actor_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(tenant_id,matter_id,id)
);
CREATE TABLE "${schema}"."${prefix}shared_material" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 matter_id TEXT NOT NULL,
 publisher_id TEXT NOT NULL,
 FOREIGN KEY(tenant_id,matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,publisher_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(tenant_id,matter_id,id)
);
CREATE TABLE "${schema}"."${prefix}shared_material_revision" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 matter_id TEXT NOT NULL,
 material_id TEXT NOT NULL,
 revision BIGINT NOT NULL CHECK(revision>=1),
 publisher_id TEXT NOT NULL,
 source_user_id TEXT,
 FOREIGN KEY(tenant_id,matter_id,material_id) REFERENCES "${schema}"."${prefix}shared_material"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,publisher_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,source_user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(tenant_id,matter_id,id),
 UNIQUE(tenant_id,material_id,revision)
);
CREATE TABLE "${schema}"."${prefix}shared_publication" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 matter_id TEXT NOT NULL,
 material_id TEXT NOT NULL,
 publisher_id TEXT NOT NULL,
 FOREIGN KEY(tenant_id,matter_id,material_id) REFERENCES "${schema}"."${prefix}shared_material"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,publisher_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}shared_context" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 user_id TEXT NOT NULL,
 runtime_session_id TEXT,
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,runtime_session_id) REFERENCES "${schema}"."${prefix}session"(tenant_id,session_id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(tenant_id,user_id,id)
);
CREATE TABLE "${schema}"."${prefix}shared_source_dependency" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 user_id TEXT NOT NULL,
 context_id TEXT NOT NULL,
 matter_id TEXT NOT NULL,
 material_revision_id TEXT,
 grant_id TEXT NOT NULL,
 workspace_message_id BIGINT,
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id,material_revision_id) REFERENCES "${schema}"."${prefix}shared_material_revision"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,user_id,context_id) REFERENCES "${schema}"."${prefix}shared_context"(tenant_id,user_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id,grant_id) REFERENCES "${schema}"."${prefix}shared_access_grant"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,user_id,workspace_message_id) REFERENCES "${schema}"."${prefix}workspace_message"(tenant_id,user_id,message_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}shared_notification" (
 tenant_id TEXT NOT NULL,
 id TEXT NOT NULL,
 record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 user_id TEXT NOT NULL,
 matter_id TEXT NOT NULL,
 event_id TEXT NOT NULL,
 semantic_key TEXT NOT NULL,
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id,event_id) REFERENCES "${schema}"."${prefix}shared_event"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 UNIQUE(tenant_id,user_id,semantic_key)
);
CREATE TABLE "${schema}"."${prefix}shared_grant_revision" (
 tenant_id TEXT NOT NULL, matter_id TEXT NOT NULL, grant_id TEXT NOT NULL, material_revision_id TEXT NOT NULL,
 PRIMARY KEY(tenant_id,grant_id,material_revision_id),
 FOREIGN KEY(tenant_id,matter_id,grant_id) REFERENCES "${schema}"."${prefix}shared_access_grant"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,matter_id,material_revision_id) REFERENCES "${schema}"."${prefix}shared_material_revision"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}shared_material_source" (
 tenant_id TEXT NOT NULL, material_revision_id TEXT NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal>=0), source_matter_id TEXT NOT NULL, source_revision_id TEXT,
 PRIMARY KEY(tenant_id,material_revision_id,ordinal),
 FOREIGN KEY(tenant_id,material_revision_id) REFERENCES "${schema}"."${prefix}shared_material_revision"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,source_matter_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,source_matter_id,source_revision_id) REFERENCES "${schema}"."${prefix}shared_material_revision"(tenant_id,matter_id,id) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE "${schema}"."${prefix}workspace_message" ADD COLUMN sources JSONB NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(sources)='array');
