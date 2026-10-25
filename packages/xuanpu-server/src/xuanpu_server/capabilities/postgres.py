"""能力表使用既有协调事务连接，登记和回执不会分别提交。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import (
    CapabilityRevision,
    CapabilityState,
    Installation,
    InstallationConfig,
    InstallPreview,
)

if TYPE_CHECKING:
    import asyncpg
    from asyncpg.pool import PoolConnectionProxy
    from pydantic import BaseModel
    from taifeng_stores import SqlNamespace

    Connection = asyncpg.Connection | PoolConnectionProxy

TABLES: tuple[tuple[str, str, type[BaseModel], tuple[str, ...]], ...] = (
    ('revisions', 'capability_revision', CapabilityRevision, ('capability_id', 'revision')),
    ('previews', 'capability_preview', InstallPreview, ('capability_id', 'revision')),
    ('configurations', 'capability_config', InstallationConfig, ('capability_id', 'revision')),
    ('installations', 'capability_installation', Installation, ('capability_id', 'config_id')),
)


async def load_capabilities(conn: Connection, namespace: SqlNamespace, tenant: str) -> CapabilityState:
    """所有查询显式限定租户，损坏JSON不会降级成空目录。"""
    values = {}
    for name, table, model, _ in TABLES:
        rows = await conn.fetch(f'SELECT id,record FROM {namespace.table(table)} WHERE tenant_id=$1', tenant)
        values[name] = {row['id']: model.model_validate_json(row['record']) for row in rows}
    return CapabilityState.model_validate(values)


async def save_capabilities(conn: Connection, namespace: SqlNamespace, tenant: str,
                            before: CapabilityState, after: CapabilityState) -> None:
    """先持久能力主键，再按引用顺序保存修订、预检、配置和当前安装。"""
    for key in after.revisions.keys() - before.revisions.keys():
        revision = after.revisions[key]
        await conn.execute(f'INSERT INTO {namespace.table("capability_root")} '
            '(tenant_id,id,capability_key,package_name,entry) VALUES($1,$2,$3,$4,$5) '
            'ON CONFLICT (tenant_id,id) DO NOTHING', tenant, revision.capability_id,
            revision.capability_key, revision.delivery.package.name, revision.delivery.package.entry)
    for name, table, _, columns in TABLES:
        old: dict[str, BaseModel] = getattr(before, name)
        new: dict[str, BaseModel] = getattr(after, name)
        for key, record in new.items():
            if old.get(key) == record:
                continue
            fields = ('tenant_id', 'id', *columns, 'record')
            parameters = ','.join('$' + str(i + 1) for i in range(len(fields)))
            updates = ','.join(f'{field}=EXCLUDED.{field}' for field in (*columns, 'record'))
            await conn.execute(f'INSERT INTO {namespace.table(table)} ({",".join(fields)}) '
                f'VALUES({parameters}) ON CONFLICT(tenant_id,id) DO UPDATE SET {updates}',
                tenant, key, *(getattr(record, field) for field in columns), record.model_dump_json())
