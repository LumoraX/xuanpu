"""人物与关系的 PostgreSQL 存储实现，与控制面共用表命名空间与锁序。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from server.core.controlplane.schema import DEFAULT_NAMESPACE
from server.core.errors import PermissionDeniedError
from server.domains.people.models import PeopleState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import asyncpg
    from taifeng_stores import SqlNamespace


def _person_from_row(row: Any) -> dict[str, Any]:
    """将数据库行反序列化为领域人物记录。"""
    rec: dict[str, Any] = {
        "id": row["person_id"],
        "name": row["name"],
        "organization": row["organization"],
        "birth_year": row["birth_year"],
        "birth_month": row["birth_month"],
        "birth_day": row["birth_day"],
        "profile": json.loads(row["profile"]),
        "avatar": json.loads(row["avatar"]) if row["avatar"] is not None else None,
        "version": row["version"],
        "merged_into_id": row["merged_into_id"],
        "merge_snapshot": json.loads(row["merge_snapshot"]) if row["merge_snapshot"] is not None else None,
        "trashed_at": row["trashed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["trash_batch"] is not None:
        rec["_trash_batch"] = row["trash_batch"]
    return rec


def _relation_from_row(row: Any) -> dict[str, Any]:
    """将数据库行反序列化为领域关系记录。"""
    rec: dict[str, Any] = {
        "id": row["relation_id"],
        "from_person_id": row["from_person_id"],
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "relation_type": row["relation_type"],
        "symmetric": row["is_symmetric"],
        "role": row["role"],
        "note": row["note"],
        "valid_from": row["valid_from"],
        "valid_to": row["valid_to"],
        "source_note_id": row["source_note_id"],
        "version": row["version"],
        "trashed_at": row["trashed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if row["trash_batch"] is not None:
        rec["_trash_batch"] = row["trash_batch"]
    return rec


def _note_from_row(row: Any) -> dict[str, Any]:
    """将数据库行反序列化为领域原始互动记录。"""
    return {
        "id": row["note_id"],
        "occurred_at": row["occurred_at"],
        "content": row["content"],
        "person_ids": json.loads(row["person_ids"]),
        "matter_id": row["matter_id"],
        "attachment_file_ids": json.loads(row["attachment_file_ids"]),
        "source_message_id": row["source_message_id"],
        "version": row["version"],
        "trashed_at": row["trashed_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class PostgresPeopleStore:
    """租户共享锁、员工排他锁后原子提交人物、关系、笔记与脱敏审计。"""

    def __init__(self, pool: asyncpg.Pool, *, namespace: SqlNamespace = DEFAULT_NAMESPACE) -> None:
        """表名来自已验证的 SqlNamespace，业务数据全部使用参数绑定。"""
        self._pool = pool
        self._t = {
            name: namespace.table(name)
            for name in (
                "tenant",
                "user",
                "audit",
                "personal_person",
                "personal_relation",
                "personal_note",
                "personal_people_request",
            )
        }

    async def _lock(self, conn: Any, tenant: str, user: str) -> None:
        """沿用工作台锁序：先租户共享锁、再员工排他锁。"""
        t_status = await conn.fetchval(
            f"SELECT status FROM {self._t['tenant']} WHERE tenant_id=$1 FOR SHARE", tenant
        )
        u_status = await conn.fetchval(
            f"SELECT status FROM {self._t['user']} WHERE tenant_id=$1 AND user_id=$2 FOR UPDATE",
            tenant,
            user,
        )
        if t_status != "active" or u_status != "active":
            raise PermissionDeniedError("员工或租户不可用")

    async def _load(self, conn: Any, tenant: str, user: str) -> PeopleState:
        """装载单员工完整的人物空间快照。"""
        state = PeopleState()
        for row in await conn.fetch(
            f"SELECT person_id,name,organization,birth_year,birth_month,birth_day,profile,avatar,"
            f"version,merged_into_id,merge_snapshot,trashed_at,trash_batch,created_at,updated_at "
            f"FROM {self._t['personal_person']} WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        ):
            rec = _person_from_row(row)
            state.persons[rec["id"]] = rec
        for row in await conn.fetch(
            f"SELECT relation_id,from_person_id,target_kind,target_id,relation_type,is_symmetric,"
            f"role,note,valid_from,valid_to,source_note_id,version,trashed_at,trash_batch,"
            f"created_at,updated_at FROM {self._t['personal_relation']} "
            f"WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        ):
            rec = _relation_from_row(row)
            state.relations[rec["id"]] = rec
        for row in await conn.fetch(
            f"SELECT note_id,occurred_at,content,person_ids,matter_id,attachment_file_ids,"
            f"source_message_id,version,trashed_at,created_at,updated_at "
            f"FROM {self._t['personal_note']} WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        ):
            rec = _note_from_row(row)
            state.notes[rec["id"]] = rec
        for row in await conn.fetch(
            f"SELECT request_id,fingerprint,result FROM {self._t['personal_people_request']} "
            f"WHERE tenant_id=$1 AND user_id=$2",
            tenant,
            user,
        ):
            state.requests[row["request_id"]] = {
                "fingerprint": row["fingerprint"],
                "result": json.loads(row["result"]),
            }
        return state

    async def snapshot(self, tenant: str, user: str) -> PeopleState:
        """无写锁读取本人快照。"""
        async with self._pool.acquire() as conn:
            return await self._load(conn, tenant, user)

    async def _save_persons(
        self, conn: Any, tenant: str, user: str, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
    ) -> None:
        """先写主档案再写被合并档案，保证自引用外键顺序始终成立。"""
        changed = [rec for pid, rec in after.items() if before.get(pid) != rec]
        changed.sort(key=lambda r: 0 if r.get("merged_into_id") is None else 1)
        sql = (
            f"INSERT INTO {self._t['personal_person']} "
            f"(tenant_id,user_id,person_id,name,organization,birth_year,birth_month,birth_day,"
            f"profile,avatar,version,merged_into_id,merge_snapshot,trashed_at,trash_batch,"
            f"created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,"
            f"$11,$12,$13::jsonb,$14,$15,$16,$17) "
            f"ON CONFLICT (tenant_id,user_id,person_id) DO UPDATE SET "
            f"name=EXCLUDED.name,organization=EXCLUDED.organization,birth_year=EXCLUDED.birth_year,"
            f"birth_month=EXCLUDED.birth_month,birth_day=EXCLUDED.birth_day,profile=EXCLUDED.profile,"
            f"avatar=EXCLUDED.avatar,version=EXCLUDED.version,merged_into_id=EXCLUDED.merged_into_id,"
            f"merge_snapshot=EXCLUDED.merge_snapshot,trashed_at=EXCLUDED.trashed_at,"
            f"trash_batch=EXCLUDED.trash_batch,updated_at=EXCLUDED.updated_at"
        )
        for rec in changed:
            await conn.execute(
                sql,
                tenant,
                user,
                rec["id"],
                rec["name"],
                rec["organization"],
                rec["birth_year"],
                rec["birth_month"],
                rec["birth_day"],
                json.dumps(rec["profile"], ensure_ascii=False),
                json.dumps(rec["avatar"], ensure_ascii=False) if rec["avatar"] is not None else None,
                rec["version"],
                rec["merged_into_id"],
                json.dumps(rec["merge_snapshot"], ensure_ascii=False) if rec["merge_snapshot"] is not None else None,
                rec["trashed_at"],
                rec.get("_trash_batch"),
                rec["created_at"],
                rec["updated_at"],
            )

    async def _save_relations_and_notes(
        self, conn: Any, tenant: str, user: str, before: PeopleState, after: PeopleState
    ) -> None:
        """持久化发生变更的关系边、原始笔记与幂等回执。"""
        rel_sql = (
            f"INSERT INTO {self._t['personal_relation']} "
            f"(tenant_id,user_id,relation_id,from_person_id,target_kind,target_id,relation_type,"
            f"is_symmetric,role,note,valid_from,valid_to,source_note_id,version,trashed_at,"
            f"trash_batch,created_at,updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,"
            f"$12,$13,$14,$15,$16,$17,$18) "
            f"ON CONFLICT (tenant_id,user_id,relation_id) DO UPDATE SET "
            f"from_person_id=EXCLUDED.from_person_id,target_kind=EXCLUDED.target_kind,"
            f"target_id=EXCLUDED.target_id,relation_type=EXCLUDED.relation_type,"
            f"is_symmetric=EXCLUDED.is_symmetric,role=EXCLUDED.role,note=EXCLUDED.note,"
            f"valid_from=EXCLUDED.valid_from,valid_to=EXCLUDED.valid_to,"
            f"source_note_id=EXCLUDED.source_note_id,version=EXCLUDED.version,"
            f"trashed_at=EXCLUDED.trashed_at,trash_batch=EXCLUDED.trash_batch,"
            f"updated_at=EXCLUDED.updated_at"
        )
        for rid, rec in after.relations.items():
            if before.relations.get(rid) == rec:
                continue
            await conn.execute(
                rel_sql,
                tenant,
                user,
                rec["id"],
                rec["from_person_id"],
                rec["target_kind"],
                rec["target_id"],
                rec["relation_type"],
                rec["symmetric"],
                rec["role"],
                rec["note"],
                rec["valid_from"],
                rec["valid_to"],
                rec["source_note_id"],
                rec["version"],
                rec["trashed_at"],
                rec.get("_trash_batch"),
                rec["created_at"],
                rec["updated_at"],
            )
        note_sql = (
            f"INSERT INTO {self._t['personal_note']} "
            f"(tenant_id,user_id,note_id,occurred_at,content,person_ids,matter_id,"
            f"attachment_file_ids,source_message_id,version,trashed_at,created_at,updated_at) "
            f"VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8::jsonb,$9,$10,$11,$12,$13) "
            f"ON CONFLICT (tenant_id,user_id,note_id) DO UPDATE SET "
            f"occurred_at=EXCLUDED.occurred_at,content=EXCLUDED.content,"
            f"person_ids=EXCLUDED.person_ids,matter_id=EXCLUDED.matter_id,"
            f"attachment_file_ids=EXCLUDED.attachment_file_ids,version=EXCLUDED.version,"
            f"trashed_at=EXCLUDED.trashed_at,updated_at=EXCLUDED.updated_at"
        )
        for nid, rec in after.notes.items():
            if before.notes.get(nid) == rec:
                continue
            await conn.execute(
                note_sql,
                tenant,
                user,
                rec["id"],
                rec["occurred_at"],
                rec["content"],
                json.dumps(rec["person_ids"], ensure_ascii=False),
                rec["matter_id"],
                json.dumps(rec["attachment_file_ids"], ensure_ascii=False),
                rec["source_message_id"],
                rec["version"],
                rec["trashed_at"],
                rec["created_at"],
                rec["updated_at"],
            )
        req_sql = (
            f"INSERT INTO {self._t['personal_people_request']} "
            f"(tenant_id,user_id,request_id,fingerprint,result) VALUES ($1,$2,$3,$4,$5::jsonb) "
            f"ON CONFLICT (tenant_id,user_id,request_id) DO NOTHING"
        )
        for req_id, rec in after.requests.items():
            if req_id in before.requests:
                continue
            await conn.execute(
                req_sql,
                tenant,
                user,
                req_id,
                rec["fingerprint"],
                json.dumps(rec["result"], ensure_ascii=False),
            )

    @asynccontextmanager
    async def transaction(
        self,
        tenant: str,
        user: str,
        guard: Callable[[], Awaitable[None]],
    ) -> AsyncIterator[PeopleState]:
        """在单个 PostgreSQL 事务内加锁、校验、写入增量状态与脱敏审计。"""
        async with self._pool.acquire() as conn, conn.transaction():
            await self._lock(conn, tenant, user)
            await guard()
            before = await self._load(conn, tenant, user)
            working = deepcopy(before)
            yield working
            await self._save_persons(conn, tenant, user, before.persons, working.persons)
            await self._save_relations_and_notes(conn, tenant, user, before, working)
            for entry in working.audit:
                await conn.execute(
                    f"INSERT INTO {self._t['audit']} (tenant_id,user_id,source,kind,at,actor,data) "
                    f"VALUES ($1,$2,'control',$3,$4,$5,$6::jsonb)",
                    tenant,
                    user,
                    entry["kind"],
                    entry["at"],
                    "user:" + user,
                    json.dumps(entry["data"], ensure_ascii=False),
                )
