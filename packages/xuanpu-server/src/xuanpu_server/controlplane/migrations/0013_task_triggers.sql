-- 执行安排、确认版本与发生实例复用员工身份、独立授权及原任务外键。
CREATE TABLE "${schema}"."${prefix}task_definition" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,definition_id TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version>0),enabled BOOLEAN NOT NULL,next_check_at DOUBLE PRECISION,
 record JSONB NOT NULL,PRIMARY KEY(tenant_id,user_id,definition_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE INDEX "${prefix}task_definition_due" ON "${schema}"."${prefix}task_definition"(enabled,next_check_at,tenant_id,user_id,definition_id);
CREATE TABLE "${schema}"."${prefix}task_definition_version" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,revision_key TEXT NOT NULL,
 definition_id TEXT NOT NULL,version INTEGER NOT NULL CHECK(version>0),grant_id TEXT NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,user_id,revision_key),UNIQUE(tenant_id,user_id,definition_id,version),
 FOREIGN KEY(tenant_id,user_id,definition_id) REFERENCES "${schema}"."${prefix}task_definition"(tenant_id,user_id,definition_id),
 FOREIGN KEY(tenant_id,user_id,grant_id) REFERENCES "${schema}"."${prefix}task_grant"(tenant_id,user_id,grant_id)
);
CREATE TABLE "${schema}"."${prefix}task_definition_input" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,definition_id TEXT NOT NULL,version INTEGER NOT NULL,
 item_id TEXT NOT NULL,revision INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,user_id,definition_id,version,item_id),
 FOREIGN KEY(tenant_id,user_id,definition_id,version) REFERENCES "${schema}"."${prefix}task_definition_version"(tenant_id,user_id,definition_id,version),
 FOREIGN KEY(tenant_id,user_id,item_id,revision) REFERENCES "${schema}"."${prefix}workspace_file_version"(tenant_id,user_id,item_id,revision)
);
CREATE TABLE "${schema}"."${prefix}task_occurrence" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,occurrence_id TEXT NOT NULL,
 definition_id TEXT NOT NULL,definition_version INTEGER NOT NULL,occurrence_key TEXT NOT NULL,
 task_id TEXT,status TEXT NOT NULL CHECK(status IN ('pending','accepted','missed','skipped_overlap','blocked','cancelled')),
 record JSONB NOT NULL,PRIMARY KEY(tenant_id,user_id,occurrence_id),
 UNIQUE(tenant_id,user_id,definition_id,definition_version,occurrence_key),
 FOREIGN KEY(tenant_id,user_id,definition_id,definition_version) REFERENCES "${schema}"."${prefix}task_definition_version"(tenant_id,user_id,definition_id,version),
 FOREIGN KEY(tenant_id,user_id,task_id) REFERENCES "${schema}"."${prefix}work_task"(tenant_id,user_id,task_id)
);
CREATE TABLE "${schema}"."${prefix}task_occurrence_input" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,occurrence_id TEXT NOT NULL,item_id TEXT NOT NULL,revision INTEGER NOT NULL,
 PRIMARY KEY(tenant_id,user_id,occurrence_id,item_id),
 FOREIGN KEY(tenant_id,user_id,occurrence_id) REFERENCES "${schema}"."${prefix}task_occurrence"(tenant_id,user_id,occurrence_id),
 FOREIGN KEY(tenant_id,user_id,item_id,revision) REFERENCES "${schema}"."${prefix}workspace_file_version"(tenant_id,user_id,item_id,revision)
);
-- 序号只作诊断，投递扫描绝不能用最大sequence_id当提交高水位。
CREATE TABLE "${schema}"."${prefix}task_domain_event" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,event_id TEXT NOT NULL,
 sequence_id BIGSERIAL UNIQUE,event_type TEXT NOT NULL,occurred_at DOUBLE PRECISION NOT NULL,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,user_id,event_id),
 FOREIGN KEY(tenant_id,user_id) REFERENCES "${schema}"."${prefix}user"(tenant_id,user_id)
);
CREATE TABLE "${schema}"."${prefix}task_event_delivery" (
 tenant_id TEXT NOT NULL,user_id TEXT COLLATE "C" NOT NULL,delivery_id TEXT NOT NULL,event_id TEXT NOT NULL,
 definition_id TEXT NOT NULL,definition_version INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('pending','claimed','settled','blocked')),
 next_check_at DOUBLE PRECISION NOT NULL,lease_until DOUBLE PRECISION NOT NULL,occurrence_id TEXT,record JSONB NOT NULL,
 PRIMARY KEY(tenant_id,user_id,delivery_id),UNIQUE(tenant_id,user_id,event_id,definition_id,definition_version),
 FOREIGN KEY(tenant_id,user_id,event_id) REFERENCES "${schema}"."${prefix}task_domain_event"(tenant_id,user_id,event_id),
 FOREIGN KEY(tenant_id,user_id,definition_id,definition_version) REFERENCES "${schema}"."${prefix}task_definition_version"(tenant_id,user_id,definition_id,version),
 FOREIGN KEY(tenant_id,user_id,occurrence_id) REFERENCES "${schema}"."${prefix}task_occurrence"(tenant_id,user_id,occurrence_id)
);
CREATE INDEX "${prefix}task_delivery_due" ON "${schema}"."${prefix}task_event_delivery"(status,next_check_at,lease_until,tenant_id,user_id,delivery_id);

-- 明确的站内执行安排暂停通知，旧三类来源约束保持。
ALTER TABLE "${schema}"."${prefix}notification"
 ADD COLUMN task_definition_id TEXT,
 DROP CONSTRAINT "${prefix}notification_check",
 ADD CONSTRAINT "${prefix}notification_source_check" CHECK (
 (source='reminder' AND reminder_id IS NOT NULL AND due_at IS NOT NULL AND proactive_check_id IS NULL AND proactive_followup_id IS NULL AND task_definition_id IS NULL)
 OR (source='proactive_check' AND reminder_id IS NULL AND proactive_check_id IS NOT NULL AND proactive_followup_id IS NULL AND task_definition_id IS NULL AND notification_key IS NOT NULL)
 OR (source='proactive_followup' AND reminder_id IS NULL AND proactive_followup_id IS NOT NULL AND proactive_check_id IS NULL AND task_definition_id IS NULL AND notification_key IS NOT NULL)
 OR (source='task_definition' AND reminder_id IS NULL AND proactive_check_id IS NULL AND proactive_followup_id IS NULL AND task_definition_id IS NOT NULL AND notification_key IS NOT NULL)
 ),
 ADD FOREIGN KEY(tenant_id,user_id,task_definition_id) REFERENCES "${schema}"."${prefix}task_definition"(tenant_id,user_id,definition_id);
