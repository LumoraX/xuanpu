"""微内核插件零 DDL 扩展集合的控制面存储后端（Memory 与 PostgreSQL 双实现，ADR 0027）。

- PostgreSQL 后端把插件集合记录存入 ``plugin_collection`` 表（迁移 ``0040``），实现多节点共享与 ACID 乐观锁；
- 内存后端配合 ``PluginCollectionStore`` 的本地磁盘原子落盘使用，保证单机试用重启同样不丢数据。
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, Protocol

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.plugins.models import PluginCollectionItem

if TYPE_CHECKING:
    import asyncpg
    from taifeng_stores import SqlNamespace

_MAX_ITEMS_PER_COLLECTION = 1000


class PluginCollectionBackend(Protocol):
    """控制面统一的插件扩展集合持久化协议。"""

    is_shared: bool

    async def list_items(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, *, limit: int = 100,
    ) -> list[PluginCollectionItem]:
        """按更新时间倒序返回指定命名空间内的记录。"""
        ...

    async def get_item(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, record_id: str,
    ) -> PluginCollectionItem:
        """按主键取单条记录；不存在抛出 NotFoundError。"""
        ...

    async def put_item(
        self,
        *,
        tenant_id: str,
        plugin_id: str,
        user_id: str,
        collection: str,
        record_id: str,
        data: dict[str, Any],
        expected_version: int | None,
        now: float,
    ) -> PluginCollectionItem:
        """新建或按乐观锁更新单条记录；版本冲突抛出 ConflictError。"""
        ...

    async def delete_item(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, record_id: str,
    ) -> bool:
        """删除单条记录；不存在抛出 NotFoundError。"""
        ...


class MemoryPluginCollectionBackend:
    """进程内插件集合后端（``is_shared=False``，由上层按需叠加本地磁盘落盘）。"""

    is_shared = False

    def __init__(self) -> None:
        """初始化内存表与锁。"""
        self._lock = asyncio.Lock()
        self._rows: dict[tuple[str, str, str, str, str], PluginCollectionItem] = {}

    async def list_items(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, *, limit: int = 100,
    ) -> list[PluginCollectionItem]:
        """列出同集合记录（按更新时间倒序）。"""
        rows = [
            item for (t, p, u, c, _), item in self._rows.items()
            if t == tenant_id and p == plugin_id and u == user_id and c == collection
        ]
        rows.sort(key=lambda r: r.updated_at, reverse=True)
        return rows[: max(1, min(limit, _MAX_ITEMS_PER_COLLECTION))]

    async def get_item(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, record_id: str,
    ) -> PluginCollectionItem:
        """读取单条记录。"""
        item = self._rows.get((tenant_id, plugin_id, user_id, collection, record_id))
        if item is None:
            raise NotFoundError("插件集合记录不存在")
        return item

    async def put_item(
        self,
        *,
        tenant_id: str,
        plugin_id: str,
        user_id: str,
        collection: str,
        record_id: str,
        data: dict[str, Any],
        expected_version: int | None,
        now: float,
    ) -> PluginCollectionItem:
        """新建或按乐观锁更新记录。"""
        key = (tenant_id, plugin_id, user_id, collection, record_id)
        async with self._lock:
            existing = self._rows.get(key)
            if existing is not None:
                if expected_version is not None and existing.version != expected_version:
                    raise ConflictError(
                        f"插件记录版本冲突（期望 {expected_version}，当前 {existing.version}）"
                    )
                updated = existing.model_copy(
                    update={"data": dict(data), "version": existing.version + 1, "updated_at": now}
                )
                self._rows[key] = updated
                return updated
            if expected_version is not None:
                raise NotFoundError("要更新的插件集合记录不存在")
            count = sum(
                1 for (t, p, u, c, _) in self._rows
                if t == tenant_id and p == plugin_id and u == user_id and c == collection
            )
            if count >= _MAX_ITEMS_PER_COLLECTION:
                raise InvalidRequestError(f"单个插件集合最多保存 {_MAX_ITEMS_PER_COLLECTION} 条记录")
            created = PluginCollectionItem(
                record_id=record_id,
                tenant_id=tenant_id,
                user_id=user_id,
                plugin_id=plugin_id,
                collection=collection,
                data=dict(data),
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._rows[key] = created
            return created

    async def delete_item(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, record_id: str,
    ) -> bool:
        """删除单条记录。"""
        key = (tenant_id, plugin_id, user_id, collection, record_id)
        async with self._lock:
            if key not in self._rows:
                raise NotFoundError("插件集合记录不存在")
            del self._rows[key]
            return True


class PostgresPluginCollectionBackend:
    """PostgreSQL 共享控制面插件集合后端（``is_shared=True``，支持多节点 ACID 乐观锁）。"""

    is_shared = True

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """绑定连接池与 SQL 命名空间。"""
        self._pool = pool
        self._table = str(namespace.table("plugin_collection"))

    @staticmethod
    def _row_to_item(row: asyncpg.Record) -> PluginCollectionItem:
        raw_data = row["data"]
        data = json.loads(raw_data) if isinstance(raw_data, str) else dict(raw_data)
        return PluginCollectionItem(
            record_id=row["record_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            plugin_id=row["plugin_id"],
            collection=row["collection"],
            data=data,
            version=int(row["version"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    async def list_items(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, *, limit: int = 100,
    ) -> list[PluginCollectionItem]:
        """查询同集合记录（按更新时间倒序）。"""
        bounded = max(1, min(limit, _MAX_ITEMS_PER_COLLECTION))
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT tenant_id, plugin_id, user_id, collection, record_id, version, data, "
                f"created_at, updated_at FROM {self._table} "
                "WHERE tenant_id=$1 AND plugin_id=$2 AND user_id=$3 AND collection=$4 "
                "ORDER BY updated_at DESC LIMIT $5",
                tenant_id, plugin_id, user_id, collection, bounded,
            )
        return [self._row_to_item(r) for r in rows]

    async def get_item(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, record_id: str,
    ) -> PluginCollectionItem:
        """按主键查询单条记录。"""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT tenant_id, plugin_id, user_id, collection, record_id, version, data, "
                f"created_at, updated_at FROM {self._table} "
                "WHERE tenant_id=$1 AND plugin_id=$2 AND user_id=$3 AND collection=$4 AND record_id=$5",
                tenant_id, plugin_id, user_id, collection, record_id,
            )
        if row is None:
            raise NotFoundError("插件集合记录不存在")
        return self._row_to_item(row)

    async def put_item(
        self,
        *,
        tenant_id: str,
        plugin_id: str,
        user_id: str,
        collection: str,
        record_id: str,
        data: dict[str, Any],
        expected_version: int | None,
        now: float,
    ) -> PluginCollectionItem:
        """在事务内执行新建或乐观锁更新。"""
        payload_json = json.dumps(data, ensure_ascii=False)
        async with self._pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                f"SELECT version, created_at FROM {self._table} "
                "WHERE tenant_id=$1 AND plugin_id=$2 AND user_id=$3 AND collection=$4 AND record_id=$5 "
                "FOR UPDATE",
                tenant_id, plugin_id, user_id, collection, record_id,
            )
            if existing is not None:
                cur_ver = int(existing["version"])
                if expected_version is not None and cur_ver != expected_version:
                    raise ConflictError(
                        f"插件记录版本冲突（期望 {expected_version}，当前 {cur_ver}）"
                    )
                new_ver = cur_ver + 1
                await conn.execute(
                    f"UPDATE {self._table} SET version=$6, data=$7::jsonb, updated_at=$8 "
                    "WHERE tenant_id=$1 AND plugin_id=$2 AND user_id=$3 AND collection=$4 AND record_id=$5",
                    tenant_id, plugin_id, user_id, collection, record_id, new_ver, payload_json, now,
                )
                return PluginCollectionItem(
                    record_id=record_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    plugin_id=plugin_id,
                    collection=collection,
                    data=dict(data),
                    version=new_ver,
                    created_at=float(existing["created_at"]),
                    updated_at=now,
                )
            if expected_version is not None:
                raise NotFoundError("要更新的插件集合记录不存在")
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {self._table} "
                "WHERE tenant_id=$1 AND plugin_id=$2 AND user_id=$3 AND collection=$4",
                tenant_id, plugin_id, user_id, collection,
            )
            if int(count or 0) >= _MAX_ITEMS_PER_COLLECTION:
                raise InvalidRequestError(f"单个插件集合最多保存 {_MAX_ITEMS_PER_COLLECTION} 条记录")
            await conn.execute(
                f"INSERT INTO {self._table} "
                "(tenant_id, plugin_id, user_id, collection, record_id, version, data, created_at, updated_at) "
                "VALUES ($1, $2, $3, $4, $5, 1, $6::jsonb, $7, $8)",
                tenant_id, plugin_id, user_id, collection, record_id, payload_json, now, now,
            )
            return PluginCollectionItem(
                record_id=record_id,
                tenant_id=tenant_id,
                user_id=user_id,
                plugin_id=plugin_id,
                collection=collection,
                data=dict(data),
                version=1,
                created_at=now,
                updated_at=now,
            )

    async def delete_item(
        self, tenant_id: str, plugin_id: str, user_id: str, collection: str, record_id: str,
    ) -> bool:
        """按主键删除单条记录。"""
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                f"DELETE FROM {self._table} "
                "WHERE tenant_id=$1 AND plugin_id=$2 AND user_id=$3 AND collection=$4 AND record_id=$5",
                tenant_id, plugin_id, user_id, collection, record_id,
            )
        if not result.endswith(" 1"):
            raise NotFoundError("插件集合记录不存在")
        return True
