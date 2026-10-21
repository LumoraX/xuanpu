-- 任务动态复用原通知表，来源外键始终包含租户及员工。
ALTER TABLE "${schema}"."${prefix}notification"
 ADD COLUMN work_task_id TEXT,
 DROP CONSTRAINT "${prefix}notification_source_check",
 ADD CONSTRAINT "${prefix}notification_source_check" CHECK (
 (source='reminder' AND reminder_id IS NOT NULL AND due_at IS NOT NULL AND proactive_check_id IS NULL AND proactive_followup_id IS NULL AND task_definition_id IS NULL AND work_task_id IS NULL)
 OR (source='proactive_check' AND reminder_id IS NULL AND proactive_check_id IS NOT NULL AND proactive_followup_id IS NULL AND task_definition_id IS NULL AND work_task_id IS NULL AND notification_key IS NOT NULL)
 OR (source='proactive_followup' AND reminder_id IS NULL AND proactive_followup_id IS NOT NULL AND proactive_check_id IS NULL AND task_definition_id IS NULL AND work_task_id IS NULL AND notification_key IS NOT NULL)
 OR (source='task_definition' AND reminder_id IS NULL AND proactive_check_id IS NULL AND proactive_followup_id IS NULL AND task_definition_id IS NOT NULL AND work_task_id IS NULL AND notification_key IS NOT NULL)
 OR (source='work_task' AND reminder_id IS NULL AND proactive_check_id IS NULL AND proactive_followup_id IS NULL AND task_definition_id IS NULL AND work_task_id IS NOT NULL AND notification_key IS NOT NULL)
 ),
 ADD FOREIGN KEY(tenant_id,user_id,work_task_id) REFERENCES "${schema}"."${prefix}work_task"(tenant_id,user_id,task_id);
