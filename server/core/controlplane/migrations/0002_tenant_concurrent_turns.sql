-- 租户配额：并发对话数上限。
ALTER TABLE "${schema}"."${prefix}tenant" ADD COLUMN max_concurrent_turns INTEGER;
