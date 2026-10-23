"""组织PG仓储只能使用协调事务提供的同一连接。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError
from xuanpu_server.organization.models import (
    AuthorityPreview,
    FieldAuthority,
    Membership,
    OrganizationState,
    OrgUnit,
    Position,
    RoleAssignment,
    SourceMapping,
    SyncPreview,
)

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from pydantic import BaseModel
    from taifeng_stores import SqlNamespace

    Connection = asyncpg.Connection | PoolConnectionProxy

# 固定映射不接受用户输入作SQL标识符；显式关系列提供数据库外键。
TABLES: tuple[tuple[str, str, type[BaseModel], tuple[str, ...]], ...] = (
    ('units', 'organization_unit', OrgUnit, ('parent_id',)),
    ('positions', 'organization_position', Position, ('unit_id',)),
    ('memberships', 'organization_membership', Membership, ('user_id', 'unit_id', 'position_id')),
    ('roles', 'organization_role', RoleAssignment, ('user_id', 'scope_id', 'role')),
    ('authority', 'organization_authority', FieldAuthority, ('object_type', 'field')),
    ('authority_previews', 'organization_authority_preview', AuthorityPreview, ()),
    ('mappings', 'organization_mapping', SourceMapping, ('source', 'object_type', 'external_id', 'local_id')),
    ('previews', 'organization_preview', SyncPreview, ()),
)


async def load_organization(conn: Connection, namespace: SqlNamespace, tenant: str) -> OrganizationState:
    """从已锁定租户的当前事务读取组织数据。"""
    values: dict[str, dict[str, BaseModel]] = {}
    for name, table, model, _ in TABLES:
        rows = await conn.fetch(f'SELECT id,record FROM {namespace.table(table)} WHERE tenant_id=$1', tenant)
        values[name] = {row['id']: model.model_validate_json(row['record']) for row in rows}
    return OrganizationState.model_validate(values)


async def save_organization(conn: Connection, namespace: SqlNamespace, tenant: str,
                            old: OrganizationState, state: OrganizationState) -> None:
    """只更新改变的行；保留历史实体，禁止物理删除后复用ID。"""
    for name, table, _, columns in TABLES:
        before: dict[str, BaseModel] = getattr(old, name)
        after: dict[str, BaseModel] = getattr(state, name)
        if before.keys() - after.keys():
            raise ConflictError('组织记录只能撤销或停用，不能删除')
        for key, record in after.items():
            if before.get(key) == record:
                continue
            names = ('tenant_id', 'id', *columns, 'record')
            parameters = ','.join('$' + str(i + 1) for i in range(len(names)))
            updates = ','.join(f'{column}=EXCLUDED.{column}' for column in (*columns, 'record'))
            values = [tenant, key, *(getattr(record, column) for column in columns), record.model_dump_json()]
            await conn.execute(
                f"INSERT INTO {namespace.table(table)} ({','.join(names)}) VALUES({parameters}) "
                f"ON CONFLICT (tenant_id,id) DO UPDATE SET {updates}", *values)
