"""停机只读核验已初始化内部记录库与local加密原件。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import asyncpg

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.maintenance import maintenance_session
from xuanpu_finance.maintenance_objects import authenticate_objects
from xuanpu_finance.maintenance_types import MaintenanceError, MaintenanceOptions
from xuanpu_finance.readiness.credentials import read_fixed, read_text_secret, valid_dsn
from xuanpu_finance.storage.f9_compatibility import (
    INCOMPATIBLE_QUERIES,
    check_simulation_columns,
    check_supported_schema,
)
from xuanpu_finance.storage.f9_schema_inventory import REQUIRED_F8_SCHEMA

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection


def capture_verification_credentials(options: MaintenanceOptions) -> tuple[str, bytes]:
    """一次有界读取私有固定凭据，之后只消费已检查的内存字节。"""
    try:
        raw_dsn, _ = read_text_secret(options.database_dsn_file)
        key, _ = read_fixed(options.key_file)
        if not valid_dsn(raw_dsn):
            raise MaintenanceError('DATABASE_DSN_INVALID')
        if len(key) != 32:
            raise MaintenanceError('KEY_INVALID')
        return raw_dsn.decode('utf-8'), key
    except (OSError, ValueError) as error:
        raise MaintenanceError('MAINTENANCE_INPUT_INVALID') from error


async def check_verification_database(conn: Connection) -> None:
    """仅public完整已初始化库可核验；禁止未知schema及非清单关系。"""
    if await conn.fetchval('SELECT current_schema()') != 'public':
        raise MaintenanceError('INTERNAL_DATABASE_INCOMPATIBLE')
    extra_schema = await conn.fetchval(
        """SELECT EXISTS(SELECT 1 FROM pg_namespace
        WHERE nspname NOT IN ('public','pg_catalog','information_schema')
        AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%')""")
    relations = await conn.fetch(
        """SELECT c.relname,c.relkind::text AS relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')""")
    expected = {(name, 'r') for name in REQUIRED_F8_SCHEMA} | {('finance_event_sequence_seq', 'S')}
    if extra_schema or {(row['relname'], row['relkind']) for row in relations} != expected:
        raise MaintenanceError('INTERNAL_DATABASE_INCOMPATIBLE')
    await check_supported_schema(conn)
    for query in INCOMPATIBLE_QUERIES:
        if await conn.fetchval(query):
            raise MaintenanceError('INTERNAL_DATABASE_INCOMPATIBLE')
    await check_simulation_columns(conn)


def verification_projection(inventory: dict[str, Any]) -> dict[str, Any]:
    """只发布固定状态与计数，敏感对象键和路径不跨维护边界。"""
    complete = inventory['complete_object_validation']
    return {
        'status': 'completed',
        'verification_status': 'complete' if complete else 'incomplete',
        'profile': 'internal_records',
        'production_status': 'not_verified',
        'authenticated_objects': inventory['authenticated_objects'],
        'prepared_missing_count': len(inventory['prepared_missing']),
        'unmapped_objects_count': len(inventory['unmapped_objects']),
        'complete_object_validation': complete,
        'workers_started': False,
        'business_writes': 0,
    }


async def verify_internal_records(options: MaintenanceOptions) -> dict[str, Any]:
    """独占锁内同一只读快照核对全部结构、内容与实际AES/AAD原件。"""
    credentials = await asyncio.to_thread(capture_verification_credentials, options)
    try:
        async with maintenance_session(options, credentials=credentials) as (guard, _, key):
            async with guard.connection.transaction(isolation='repeatable_read', readonly=True):
                await check_verification_database(guard.connection)
                inventory = await authenticate_objects(guard.connection, options.object_root, key)
                result = verification_projection(inventory)
                await guard.check()
                return result
    except FinanceError as error:
        raise MaintenanceError('INTERNAL_DATABASE_INCOMPATIBLE') from error
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, ValueError, KeyError, TypeError) as error:
        raise MaintenanceError('VERIFICATION_FAILED') from error
