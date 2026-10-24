-- 到人和可选部门交接与共享原件同租户，固定责任和交付历史不能跨企业。
CREATE TABLE "${schema}"."${prefix}handoff" (
 tenant_id TEXT NOT NULL, id TEXT NOT NULL, record JSONB NOT NULL,
 shared_id TEXT NOT NULL, requester_id TEXT NOT NULL,
 target_kind TEXT NOT NULL CHECK(target_kind IN ('user','unit')),
 target_id TEXT NOT NULL, target_user_id TEXT, target_unit_id TEXT, assignee_id TEXT,
 policy_id TEXT,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,shared_id) REFERENCES "${schema}"."${prefix}shared_matter"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,requester_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,target_user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,target_unit_id) REFERENCES "${schema}"."${prefix}organization_unit"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,assignee_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 CHECK((target_kind='user' AND target_user_id=target_id AND target_user_id IS NOT NULL AND target_unit_id IS NULL)
 OR (target_kind='unit' AND target_unit_id=target_id AND target_unit_id IS NOT NULL AND target_user_id IS NULL))
);
CREATE TABLE "${schema}"."${prefix}handoff_event" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,handoff_id TEXT NOT NULL,actor_id TEXT,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,handoff_id) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,actor_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}handoff_material_ref" (
 tenant_id TEXT NOT NULL,handoff_id TEXT NOT NULL,revision_id TEXT NOT NULL,
 PRIMARY KEY(tenant_id,handoff_id,revision_id),
 FOREIGN KEY(tenant_id,handoff_id) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,revision_id) REFERENCES "${schema}"."${prefix}shared_material_revision"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}handoff_delivery" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,handoff_id TEXT NOT NULL,assignee_id TEXT NOT NULL,
 delivery_version BIGINT NOT NULL CHECK(delivery_version>0),approval_id TEXT,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,handoff_id,delivery_version),
 FOREIGN KEY(tenant_id,handoff_id) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,assignee_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE "${schema}"."${prefix}shared_access_grant" ADD COLUMN handoff_id TEXT,
 ADD COLUMN approval_id TEXT,
 ADD FOREIGN KEY(tenant_id,handoff_id) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE "${schema}"."${prefix}collaboration_notification" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,user_id TEXT NOT NULL,
 handoff_id TEXT NOT NULL,approval_id TEXT,source_kind TEXT NOT NULL,sequence BIGINT NOT NULL,
 event_id TEXT NOT NULL,semantic_key TEXT NOT NULL,
 PRIMARY KEY(tenant_id,id),UNIQUE(tenant_id,semantic_key),UNIQUE(tenant_id,sequence),
 CHECK((source_kind='handoff' AND approval_id IS NULL) OR (source_kind='approval_request' AND approval_id IS NOT NULL)),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,handoff_id) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,event_id) REFERENCES "${schema}"."${prefix}coordination_outbox"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED
);

-- 成果仍属于共享资料域，交接只限定本次新建用途。
ALTER TABLE "${schema}"."${prefix}shared_material" ADD COLUMN result_for_handoff TEXT,
 ADD FOREIGN KEY(tenant_id,result_for_handoff) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE "${schema}"."${prefix}shared_publication" ADD COLUMN result_for_handoff TEXT,
 ADD FOREIGN KEY(tenant_id,result_for_handoff) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
