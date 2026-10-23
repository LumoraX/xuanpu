"""共享PG读写只使用已锁定租户的协调连接，关系键不能跨租户。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.shared_matters.models import (
    MaterialRevision,
    Publication,
    SharedAccessGrant,
    SharedContext,
    SharedEvent,
    SharedMaterial,
    SharedMatter,
    SharedMember,
    SharedNotification,
    SharedState,
    SourceDependency,
)

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from pydantic import BaseModel
    from taifeng_stores import SqlNamespace

    Connection = asyncpg.Connection | PoolConnectionProxy

TABLES: tuple[tuple[str, str, type[BaseModel], tuple[str, ...]], ...] = (
    ('matters', 'shared_matter', SharedMatter, ('owner_id',)),
    ('grants', 'shared_access_grant', SharedAccessGrant, ('matter_id', 'subject_kind', 'subject_id', 'role_scope_id')),
    ('members', 'shared_member', SharedMember, ('matter_id', 'grant_id', 'subject_kind', 'subject_id')),
    ('history', 'shared_event', SharedEvent, ('matter_id', 'actor_id')),
    ('materials', 'shared_material', SharedMaterial, ('matter_id', 'publisher_id')),
    ('revisions', 'shared_material_revision', MaterialRevision, ('matter_id', 'material_id', 'revision', 'publisher_id', 'source_user_id')),
    ('publications', 'shared_publication', Publication, ('matter_id', 'material_id', 'publisher_id')),
    ('contexts', 'shared_context', SharedContext, ('user_id', 'runtime_session_id')),
    ('dependencies', 'shared_source_dependency', SourceDependency, ('user_id', 'context_id', 'matter_id', 'material_revision_id', 'grant_id', 'workspace_message_id')),
    ('notifications', 'shared_notification', SharedNotification, ('user_id', 'matter_id', 'event_id', 'semantic_key')),
)


async def load_shared(conn: Connection, namespace: SqlNamespace, tenant: str) -> SharedState:
    """损坏记录中止事务；不以空集合掩盖解析或数据库故障。"""
    values: dict[str, dict[str, BaseModel]] = {}
    for name, table, model, _ in TABLES:
        rows = await conn.fetch(f'SELECT id,record FROM {namespace.table(table)} WHERE tenant_id=$1', tenant)
        values[name] = {row['id']: model.model_validate_json(row['record']) for row in rows}
    return SharedState.model_validate(values)


async def save_shared(conn: Connection, namespace: SqlNamespace, tenant: str, before: SharedState, after: SharedState) -> None:
    """先更新旧成员再插入新owner，唯一负责人索引在转交中始终成立。"""
    for name, table, _, columns in TABLES:
        previous: dict[str, BaseModel] = getattr(before, name)
        current: dict[str, BaseModel] = getattr(after, name)
        changed = [(key, record) for key, record in current.items() if previous.get(key) != record]
        changed.sort(key=lambda pair: (pair[0] not in previous, pair[0]))
        for key, record in changed:
            data = {column: getattr(record, column) for column in columns}
            if isinstance(record, (SharedMember, SharedAccessGrant)):
                data.update(user_id=record.subject_id if record.subject_kind == 'user' else None,
                            unit_id=record.subject_id if record.subject_kind == 'unit' else None)
            names = ('tenant_id', 'id', *data, 'record')
            parameters = ','.join('$' + str(i + 1) for i in range(len(names)))
            updates = ','.join(f'{column}=EXCLUDED.{column}' for column in (*data, 'record'))
            await conn.execute(f"INSERT INTO {namespace.table(table)} ({','.join(names)}) VALUES({parameters}) "
                               f'ON CONFLICT (tenant_id,id) DO UPDATE SET {updates}',
                               tenant, key, *data.values(), record.model_dump_json())
    await _bridges(conn, namespace, tenant, before, after)


async def _bridges(conn: Connection, namespace: SqlNamespace, tenant: str, before: SharedState, after: SharedState) -> None:
    """限定材料与派生来源都有数据库复合外键；正文记录之外保留可核查关联。"""
    for key, grant in after.grants.items():
        if before.grants.get(key) == grant:
            continue
        old = before.grants.get(key)
        if old is not None and old.material_revisions != grant.material_revisions:
            from xuanpu_server.errors import ConflictError
            raise ConflictError('修改限定版本必须撤销后新建授权')
        for revision_id in grant.material_revisions:
            await conn.execute(f'INSERT INTO {namespace.table("shared_grant_revision")} (tenant_id,matter_id,grant_id,material_revision_id) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING', tenant, grant.matter_id, grant.id, revision_id)
    for key, revision in after.revisions.items():
        if key in before.revisions:
            continue
        for ordinal, source in enumerate(revision.source_refs):
            await conn.execute(f'INSERT INTO {namespace.table("shared_material_source")} (tenant_id,material_revision_id,ordinal,source_matter_id,source_revision_id) VALUES($1,$2,$3,$4,$5)', tenant, key, ordinal, source.shared_id, source.material_revision_id)
