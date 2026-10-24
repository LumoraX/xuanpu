"""交接和组织决定只使用协调事务持有的同一PG连接。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.approvals.models import ApprovalDecision, ApprovalPolicy, ApprovalRequest
from xuanpu_server.collaboration.models import CollaborationNotification, CollaborationState
from xuanpu_server.handoffs.models import Delivery, Handoff, HandoffEvent

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from pydantic import BaseModel
    from taifeng_stores import SqlNamespace

    Connection = asyncpg.Connection | PoolConnectionProxy

TABLES: tuple[tuple[str, str, type[BaseModel], tuple[str, ...]], ...] = (
    ('handoffs', 'handoff', Handoff, ('shared_id', 'requester_id', 'target_kind', 'target_id', 'assignee_id', 'policy_id')),
    ('events', 'handoff_event', HandoffEvent, ('handoff_id', 'actor_id')),
    ('deliveries', 'handoff_delivery', Delivery, ('handoff_id', 'assignee_id', 'delivery_version', 'approval_id')),
    ('policies', 'approval_policy', ApprovalPolicy, ()),
    ('approvals', 'approval_request', ApprovalRequest, ('policy_id', 'applicant_id', 'handoff_id')),
    ('decisions', 'approval_decision', ApprovalDecision, ('approval_id', 'actor_id', 'step_index')),
    ('notifications', 'collaboration_notification', CollaborationNotification,
        ('user_id', 'handoff_id', 'approval_id', 'source_kind', 'sequence', 'event_id', 'semantic_key')),
)


async def load_collaboration(conn: Connection, namespace: SqlNamespace, tenant: str) -> CollaborationState:
    """类型解析失败中断，不把损坏状态当作没有待审批事项。"""
    values: dict[str, dict[str, BaseModel]] = {}
    for field, table, model, _ in TABLES:
        rows = await conn.fetch(f'SELECT id,record FROM {namespace.table(table)} WHERE tenant_id=$1', tenant)
        values[field] = {row['id']: model.model_validate_json(row['record']) for row in rows}
    return CollaborationState.model_validate(values)


async def save_collaboration(conn: Connection, namespace: SqlNamespace, tenant: str,
                              before: CollaborationState, after: CollaborationState) -> None:
    """实体、固定关联、决定和通知由外层统一提交，不进行对象复制。"""
    for field, table, _, columns in TABLES:
        previous: dict[str, BaseModel] = getattr(before, field)
        current: dict[str, BaseModel] = getattr(after, field)
        for key, record in current.items():
            if previous.get(key) == record:
                continue
            values = {column: getattr(record, column) for column in columns}
            if isinstance(record, Handoff):
                values.update(target_user_id=record.target_id if record.target_kind == 'user' else None,
                    target_unit_id=record.target_id if record.target_kind == 'unit' else None)
            names = ('tenant_id', 'id', *values, 'record')
            params = ','.join('$' + str(i + 1) for i in range(len(names)))
            updates = ','.join(f'{name}=EXCLUDED.{name}' for name in (*values, 'record'))
            await conn.execute(f'INSERT INTO {namespace.table(table)} ({",".join(names)}) VALUES ({params}) '
                f'ON CONFLICT(tenant_id,id) DO UPDATE SET {updates}', tenant, key, *values.values(), record.model_dump_json())
            if isinstance(record, Handoff):
                await _materials(conn, namespace, tenant, record)


async def _materials(conn: Connection, namespace: SqlNamespace, tenant: str, handoff: Handoff) -> None:
    """当前输入精确关联替换；历史输入仍保存于不可改写事件和交付。"""
    table = namespace.table('handoff_material_ref')
    await conn.execute(f'DELETE FROM {table} WHERE tenant_id=$1 AND handoff_id=$2', tenant, handoff.id)
    for revision in handoff.material_revision_ids:
        await conn.execute(f'INSERT INTO {table}(tenant_id,handoff_id,revision_id) VALUES($1,$2,$3)', tenant, handoff.id, revision)
