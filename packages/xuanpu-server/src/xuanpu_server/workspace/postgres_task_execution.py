"""持久派发使用精确任务索引，写入复用员工原子事务。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState

# 所有表名由已验证的命名空间产生；记录键由服务器生成。
TABLES = {'task_grants': ('task_grant', 'grant_id'),
          'task_dispatch': ('task_dispatch', 'task_id'),
          'task_attempts': ('task_attempt', 'attempt_key'),
          'task_reservations': ('task_budget_reservation', 'reservation_id'),
          'task_effects': ('task_effect', 'logical_action_id')}


async def load_execution(conn: Any, tables: dict[str, str], tenant: str, user: str,
                         state: WorkspaceState, request_id: str | None) -> None:
    """仅执行事务读取账本；同grant用量不能只读当前attempt。"""
    from xuanpu_server.work_tasks.execution_context import current_execution

    binding = current_execution.get()
    if binding is not None:
        request_id = 'work-task:exec:' + binding.lease.task_id
    if request_id is None or not request_id.startswith('work-task:'):
        return
    task_id = request_id.split(':', 2)[2] if request_id.startswith(('work-task:exec:', 'work-task:budget:', 'work-task:view:')) else None
    if request_id.startswith('work-task:exec:'):
        rows = await conn.fetch(f"SELECT d.tenant_id,d.user_id,d.record,t.record->>'resources_pending' AS resources "
            f"FROM {tables['task_dispatch']} d JOIN {tables['work_task']} t USING(tenant_id,user_id,task_id) "
            "WHERE d.state='leased'")
        state.execution_active = [{**json.loads(row['record']), 'tenant_id': row['tenant_id'],
            'user_id': row['user_id'], 'resources_pending': row['resources'] == 'true'} for row in rows]
    for attr, (table, key) in TABLES.items():
        predicate = ' AND task_id=$3' if task_id and attr in {'task_dispatch', 'task_attempts', 'task_effects'} else ''
        if task_id and attr in {'task_grants', 'task_reservations'}:
            predicate = f' AND grant_id IN (SELECT grant_id FROM {tables["task_dispatch"]} WHERE tenant_id=$1 AND user_id=$2 AND task_id=$3)'
        args = (tenant, user, task_id) if predicate else (tenant, user)
        rows = await conn.fetch(f'SELECT {key},record FROM {tables[table]} '
                                'WHERE tenant_id=$1 AND user_id=$2' + predicate, *args)
        setattr(state, attr, {row[key]: json.loads(row['record']) for row in rows})

    if task_id is not None:
        keys = sorted({entry['key_id'] for entry in state.task_grants.values() if entry.get('key_id')})
        if keys:
            await conn.fetch(f"SELECT key_id FROM {tables['api_key']} WHERE tenant_id=$1 AND key_id=ANY($2::text[]) "
                             'ORDER BY key_id FOR SHARE',tenant,keys)


async def save_execution(conn: Any, tables: dict[str, str], tenant: str, user: str,
                         old: WorkspaceState, state: WorkspaceState) -> None:
    """授权先于派发和attempt保存，外键同时保证员工与任务绑定。"""
    from xuanpu_server.work_tasks.budget_store import usage_deltas

    for day, (inputs, outputs, total, turns) in usage_deltas(old, state).items():
        for table, employee in (('usage', False), ('user_usage', True)):
            usage_columns = 'tenant_id,day,user_id' if employee else 'tenant_id,day'
            args: list[Any] = [tenant, day, user] if employee else [tenant, day]
            args += [inputs, outputs, total, turns]
            placeholders = ','.join(f'${i}' for i in range(1, len(args)+1))
            await conn.execute(f'INSERT INTO {tables[table]} AS u ({usage_columns},input_tokens,output_tokens,total_tokens,turns) '
                f'VALUES ({placeholders}) ON CONFLICT ({usage_columns}) DO UPDATE SET '
                'input_tokens=u.input_tokens+EXCLUDED.input_tokens,output_tokens=u.output_tokens+EXCLUDED.output_tokens,'
                'total_tokens=u.total_tokens+EXCLUDED.total_tokens,turns=u.turns+EXCLUDED.turns', *args)
    for attr, (table, key) in TABLES.items():
        before, after = getattr(old, attr), getattr(state, attr)
        for identifier, record in after.items():
            if before.get(identifier) == record:
                continue
            columns, values = [key], [identifier]
            if attr != 'task_grants' and key != 'task_id':
                columns.append('task_id')
                values.append(record['task_id'])
            if attr == 'task_dispatch':
                columns += ['grant_id', 'state', 'due_at']
                values += [record['grant_id'], record['state'], record['due_at']]
            elif attr == 'task_attempts':
                columns.append('attempt_no')
                values.append(record['attempt_no'])
            elif attr == 'task_reservations':
                columns += ['grant_id', 'day', 'charged']
                values += [record['grant_id'], record['day'],
                           record['upper_bound'] if record['state'] == 'held' else record['input_tokens'] + record['output_tokens']]
            columns.append('record')
            values.append(json.dumps(record))
            names = ','.join(['tenant_id', 'user_id', *columns])
            placeholders = ','.join(f'${i}' + ('::jsonb' if i == len(values)+2 else '')
                                    for i in range(1, len(values)+3))
            updates = ','.join(f'{column}=EXCLUDED.{column}' for column in columns if column != key)
            await conn.execute(f'INSERT INTO {tables[table]} ({names}) VALUES ({placeholders}) '
                f'ON CONFLICT (tenant_id,user_id,{key}) DO UPDATE SET {updates}', tenant, user, *values)
