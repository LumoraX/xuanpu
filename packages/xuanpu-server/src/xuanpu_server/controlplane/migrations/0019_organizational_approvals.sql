-- 冻结顺序组织决定；申请人不能通过客户端覆盖审批主体或目标版本。
CREATE TABLE "${schema}"."${prefix}approval_policy" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id) REFERENCES "${schema}"."${prefix}tenant"(tenant_id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}approval_request" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,policy_id TEXT NOT NULL,
 applicant_id TEXT NOT NULL,handoff_id TEXT NOT NULL,
 PRIMARY KEY(tenant_id,id),
 FOREIGN KEY(tenant_id,policy_id) REFERENCES "${schema}"."${prefix}approval_policy"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,applicant_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,handoff_id) REFERENCES "${schema}"."${prefix}handoff"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE "${schema}"."${prefix}approval_decision" (
 tenant_id TEXT NOT NULL,id TEXT NOT NULL,record JSONB NOT NULL,approval_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,step_index INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,approval_id,step_index),
 FOREIGN KEY(tenant_id,approval_id) REFERENCES "${schema}"."${prefix}approval_request"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(tenant_id,actor_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id) DEFERRABLE INITIALLY DEFERRED
);
ALTER TABLE "${schema}"."${prefix}handoff" ADD FOREIGN KEY(tenant_id,policy_id)
 REFERENCES "${schema}"."${prefix}approval_policy"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE "${schema}"."${prefix}handoff_delivery" ADD FOREIGN KEY(tenant_id,approval_id)
 REFERENCES "${schema}"."${prefix}approval_request"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE "${schema}"."${prefix}collaboration_notification" ADD FOREIGN KEY(tenant_id,approval_id)
 REFERENCES "${schema}"."${prefix}approval_request"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE "${schema}"."${prefix}shared_access_grant" ADD FOREIGN KEY(tenant_id,approval_id)
 REFERENCES "${schema}"."${prefix}approval_request"(tenant_id,id) DEFERRABLE INITIALLY DEFERRED;
