"""触发索引与员工事务中的增量持久化，不另开任务主库。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xuanpu_server.workspace.models import WorkspaceState

TABLES={'task_definitions':('task_definition','definition_id'),
        'task_definition_versions':('task_definition_version','revision_key'),
        'task_occurrences':('task_occurrence','occurrence_id'),
        'task_domain_events':('task_domain_event','event_id'),
        'task_event_deliveries':('task_event_delivery','delivery_id')}


async def load(conn: Any,tables: dict[str,str],tenant: str,user: str,state: WorkspaceState,request_id: str | None) -> None:
    """普通领域写只读订阅定义；触发事务额外加载未结算投递和精确发生键。"""
    rows=await conn.fetch(f"SELECT definition_id,record FROM {tables['task_definition']} WHERE tenant_id=$1 AND user_id=$2",tenant,user)
    state.task_definitions={row['definition_id']:json.loads(row['record']) for row in rows}
    if not request_id or not request_id.startswith('work-task:trigger'):
        return
    for attr,(table,key) in TABLES.items():
        if attr=='task_definitions':
            continue
        predicate=''
        args: tuple[Any,...]=(tenant,user)
        if attr=='task_occurrences':
            occurrence_id=request_id.split(':')[-1]
            predicate=" AND (status IN ('accepted','pending','blocked') OR occurrence_id=$3)"
            args=(*args,occurrence_id)
        elif attr=='task_event_deliveries':
            predicate=" AND (status!='settled' OR delivery_id=$3)"
            args=(*args,request_id.split(':')[-1])
        elif attr=='task_domain_events':
            predicate=f" AND event_id IN (SELECT event_id FROM {tables['task_event_delivery']} WHERE tenant_id=$1 AND user_id=$2 AND (status!='settled' OR delivery_id=$3))"
            args=(*args,request_id.split(':')[-1])
        rows=await conn.fetch(f'SELECT {key},record FROM {tables[table]} WHERE tenant_id=$1 AND user_id=$2'+predicate,*args)
        setattr(state,attr,{row[key]:json.loads(row['record']) for row in rows})


def columns(attr: str,record: dict[str,Any]) -> dict[str,Any]:
    """所有列名固定在服务端；领域值只走绑定参数。"""
    if attr=='task_definitions':
        return {'version':record['version'],'enabled':record['enabled'],
                'next_check_at':record['next_eligibility_check_at'] or record['next_fire_at']}
    if attr=='task_definition_versions':
        return {key:record[key] for key in ('definition_id','version','grant_id')}
    if attr=='task_occurrences':
        return {key:record[key] for key in ('definition_id','definition_version','occurrence_key','task_id','status')}
    if attr=='task_domain_events':
        return {key:record[key] for key in ('event_type','occurred_at')}
    return {key:record[key] for key in ('event_id','definition_id','definition_version','status','next_check_at','lease_until','occurrence_id')}


async def save(conn: Any,tables: dict[str,str],tenant: str,user: str,old: WorkspaceState,state: WorkspaceState) -> None:
    """grant和任务已先保存，再提交定义版本、精确输入和去重墓碑。"""
    for attr,(table,key) in TABLES.items():
        before,after=getattr(old,attr),getattr(state,attr)
        for identifier,record in after.items():
            if before.get(identifier)==record:
                continue
            extra=columns(attr,record)
            data={key:identifier,**extra,'record':json.dumps(record)}
            names=','.join(['tenant_id','user_id',*data])
            placeholders=','.join(f'${i}'+('::jsonb' if i==len(data)+2 else '') for i in range(1,len(data)+3))
            updates=','.join(f'{col}=EXCLUDED.{col}' for col in data if col!=key)
            if attr in {'task_definition_versions','task_domain_events'}:
                conflict='DO NOTHING'
            else:
                conflict='DO UPDATE SET '+updates
            await conn.execute(f'INSERT INTO {tables[table]} ({names}) VALUES ({placeholders}) '
                f'ON CONFLICT (tenant_id,user_id,{key}) {conflict}',tenant,user,*data.values())
            if identifier not in before or before[identifier].get('input_files')!=record.get('input_files'):
                await save_inputs(conn,tables,tenant,user,attr,record)


async def save_inputs(conn: Any,tables: dict[str,str],tenant: str,user: str,attr: str,record: dict[str,Any]) -> None:
    """输入引用外键也包含员工，不依赖JSON中的自报身份。"""
    if attr=='task_definition_versions':
        table,keys,values='task_definition_input',['definition_id','version'],[record['definition_id'],record['version']]
        refs=record['prepared']['inputs']
    elif attr=='task_occurrences':
        table,keys,values='task_occurrence_input',['occurrence_id'],[record['occurrence_id']]
        refs=record['input_files']
    else:
        return
    for source in refs:
        columns=','.join(['tenant_id','user_id',*keys,'item_id','revision'])
        args=[tenant,user,*values,source['item_id'],source['revision']]
        placeholders=','.join(f'${i}' for i in range(1,len(args)+1))
        await conn.execute(f'INSERT INTO {tables[table]} ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING',*args)


async def candidates(pool: Any,tables: dict[str,str],kind: str, *, after: tuple[str,str,str] | None,limit: int,now: float) -> list[tuple[str,str,str]]:
    """仅查询due索引和逐行投递状态，不以序号高水位跳过晚提交。"""
    if kind=='time':
        table,key='task_definition','definition_id'
        predicate='enabled AND next_check_at<=$1'
    else:
        table,key='task_event_delivery','delivery_id'
        predicate="status IN ('pending','blocked','claimed') AND next_check_at<=$1 AND lease_until<=$1"
    cursor='' if after is None else f' AND (tenant_id,user_id,{key})>($3,$4,$5)'
    args: list[Any]=[now,limit]
    if after is not None:
        args.extend(after)
    async with pool.acquire() as conn:
        rows=await conn.fetch(f'SELECT tenant_id,user_id,{key} FROM {tables[table]} WHERE '+predicate+cursor+
                              f' ORDER BY tenant_id,user_id,{key} LIMIT $2',*args)
    return [(row['tenant_id'],row['user_id'],row[key]) for row in rows]


async def occurrences(pool: Any,tables: dict[str,str],tenant: str,user: str,identifier: str,after: str | None,limit: int) -> list[dict[str,Any]]:
    """发生历史仅有界分页读取，不为每次领域事务加载全部墓碑。"""
    async with pool.acquire() as conn:
        rows=await conn.fetch(f"SELECT record FROM {tables['task_occurrence']} WHERE tenant_id=$1 AND user_id=$2 AND definition_id=$3 "
            'AND ($4::text IS NULL OR occurrence_id>$4) ORDER BY occurrence_id LIMIT $5',tenant,user,identifier,after,limit)
    return [json.loads(row['record']) for row in rows]
