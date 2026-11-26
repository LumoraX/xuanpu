"""个人知识图谱扩展操作：公司/组织 CRUD 与跨实体多跳图谱全景聚合。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.core.errors import ConflictError, InvalidRequestError
from server.domains.people.graph_rules import (
    MAX_ORGS,
    build_full_graph,
    expand_entity_graph,
    find_similar_orgs,
    get_org,
    matches_org,
    new_org_record,
    sync_org_profile,
)
from server.domains.people.models import (
    public_note,
    public_org,
    public_person,
    public_relation,
    validate_org_profile,
)
from server.domains.people.rules import (
    append_audit,
    check_quota,
    check_version,
    get_note,
    get_person,
    remember,
    replay,
    request_key,
)

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.domains.people.service import PeopleService


async def list_orgs_op(
    service: PeopleService,
    who: Principal,
    *,
    q: str | None = None,
    city: str | None = None,
    tag: str | None = None,
    trashed: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按条件筛选本人的公司/组织列表（排除已合并档案）。"""
    if not 1 <= limit <= 100:
        raise InvalidRequestError("limit 必须在 1 到 100 之间")
    tenant, user = await service._workspace.require_employee(who)
    state = await service._store.snapshot(tenant, user)
    items = [
        rec
        for rec in state.orgs.values()
        if rec["merged_into_id"] is None
        and (rec["trashed_at"] is not None) == trashed
        and matches_org(rec, q=q, city=city, tag=tag)
    ]
    items.sort(key=lambda r: (-r["updated_at"], r["id"]))
    return [public_org(r) for r in items[:limit]]


async def get_org_op(service: PeopleService, who: Principal, org_id: str) -> dict[str, Any]:
    """读取单个公司/组织档案、相关跨实体关系与原始记录。"""
    tenant, user = await service._workspace.require_employee(who)
    state = await service._store.snapshot(tenant, user)
    rec = get_org(state, org_id)
    if rec["merged_into_id"] is not None:
        rec = get_org(state, rec["merged_into_id"])
    oid = rec["id"]
    rels = [
        public_relation(r)
        for r in state.relations.values()
        if r["trashed_at"] is None
        and (
            (r.get("from_kind", "person") == "org" and r["from_person_id"] == oid)
            or (r["target_kind"] == "org" and r["target_id"] == oid)
        )
    ]
    records = [
        public_note(n)
        for n in state.notes.values()
        if n["trashed_at"] is None and oid in n.get("org_ids", [])
    ]
    records.sort(key=lambda n: (-n["occurred_at"], n["id"]))
    return {"org": public_org(rec), "relations": rels, "records": records}


async def create_org_op(
    service: PeopleService, who: Principal, raw: dict[str, Any], request_id: str
) -> dict[str, Any]:
    """幂等创建纯个人空间公司/组织档案。"""
    key = request_key(request_id)
    profile = validate_org_profile(raw)
    tenant, user = await service._workspace.require_employee(who)
    payload = {"op": "create_org", "profile": profile}

    async def guard() -> None:
        await service._workspace.require_employee(who)

    async with service._store.transaction(tenant, user, guard) as state:
        if (cached := replay(state, key, payload)) is not None:
            return cached
        check_quota(state.orgs, MAX_ORGS, "公司/组织档案")
        now = service._now()
        rec = new_org_record(profile, now)
        state.orgs[rec["id"]] = rec
        append_audit(state, "people.org.created", "org", rec["id"], 1, now)
        result = public_org(rec)
        remember(state, key, payload, result)
        return result


async def update_org_op(
    service: PeopleService, who: Principal, org_id: str, raw: dict[str, Any], version: int
) -> dict[str, Any]:
    """带版本 CAS 更新公司/组织档案。"""
    profile = validate_org_profile(raw)
    tenant, user = await service._workspace.require_employee(who)

    async def guard() -> None:
        await service._workspace.require_employee(who)

    async with service._store.transaction(tenant, user, guard) as state:
        rec = get_org(state, org_id, active=True)
        check_version(rec, version)
        sync_org_profile(rec, profile, service._now())
        append_audit(state, "people.org.updated", "org", org_id, rec["version"], rec["updated_at"])
        return public_org(rec)


async def trash_org_op(service: PeopleService, who: Principal, org_id: str, version: int) -> dict[str, Any]:
    """软删除公司/组织档案。"""
    tenant, user = await service._workspace.require_employee(who)

    async def guard() -> None:
        await service._workspace.require_employee(who)

    async with service._store.transaction(tenant, user, guard) as state:
        rec = get_org(state, org_id, active=True)
        check_version(rec, version)
        now = service._now()
        rec["trashed_at"] = now
        rec["version"] += 1
        rec["updated_at"] = now
        append_audit(state, "people.org.trashed", "org", org_id, rec["version"], now)
        return public_org(rec)


async def restore_org_op(service: PeopleService, who: Principal, org_id: str, version: int) -> dict[str, Any]:
    """恢复已软删除的公司/组织档案。"""
    tenant, user = await service._workspace.require_employee(who)

    async def guard() -> None:
        await service._workspace.require_employee(who)

    async with service._store.transaction(tenant, user, guard) as state:
        rec = get_org(state, org_id)
        if rec["trashed_at"] is None:
            raise ConflictError("该公司档案未在回收站中")
        check_version(rec, version)
        now = service._now()
        rec["trashed_at"] = None
        rec["version"] += 1
        rec["updated_at"] = now
        append_audit(state, "people.org.restored", "org", org_id, rec["version"], now)
        return public_org(rec)


async def find_org_candidates_op(
    service: PeopleService, who: Principal, profile: dict[str, Any]
) -> list[dict[str, Any]]:
    """查找已有疑似同名或简称一致的公司/组织。"""
    tenant, user = await service._workspace.require_employee(who)
    state = await service._store.snapshot(tenant, user)
    return find_similar_orgs(state, profile)


async def entity_graph_op(
    service: PeopleService, who: Principal, entity_kind: str, entity_id: str, *, depth: int = 2
) -> dict[str, Any]:
    """跨实体 1–3 跳个人知识图谱查询，并附带事项全景（待办/日程/提醒）与溯源记录。"""
    if entity_kind not in ("person", "org", "matter", "record"):
        raise InvalidRequestError("entity_kind 仅支持 person、org、matter 或 record")
    tenant, user = await service._workspace.require_employee(who)
    state = await service._store.snapshot(tenant, user)
    matters = await service._workspace.list(who, kind="matter")
    matters_by_id = {m["id"]: m for m in matters}
    if entity_kind == "record":
        rec = get_note(state, entity_id, active=True)
        pub = public_note(rec)
        return {
            "center": {"kind": "record", "id": entity_id},
            "record": pub,
            "orgs": [public_org(state.orgs[oid]) for oid in pub["org_ids"] if oid in state.orgs],
            "persons": [public_person(state.persons[pid]) for pid in pub["person_ids"] if pid in state.persons],
            "matters": [matters_by_id[mid] for mid in pub["matter_ids"] if mid in matters_by_id],
            "relations": [
                public_relation(r)
                for r in state.relations.values()
                if r["trashed_at"] is None and r.get("source_note_id") == entity_id
            ],
        }
    if entity_kind == "person":
        get_person(state, entity_id, active=True)
    elif entity_kind == "org":
        get_org(state, entity_id, active=True)
    else:
        await service._require_matter(who, entity_id)
    out = expand_entity_graph(state, entity_kind, entity_id, depth=depth, matters_by_id=matters_by_id)
    if entity_kind == "matter":
        todos = await service._workspace.list(who, kind="todo")
        events = await service._workspace.list(who, kind="event")
        out["todos"] = [t for t in todos if (t.get("data") or {}).get("matter_id") == entity_id]
        out["events"] = [e for e in events if (e.get("data") or {}).get("matter_id") == entity_id]
        if service._reminders is not None:
            rem_ids = {rid for rec in out["records"] for rid in rec.get("reminder_ids", [])}
            active_rems = await service._reminders.list(who, status="active")
            out["reminders"] = [
                {"reminder_id": r.reminder_id, "title": r.title, "due_at": r.due_at, "status": r.status}
                for r in active_rems
                if r.reminder_id in rem_ids
            ]
    return out


async def full_graph_op(
    service: PeopleService,
    who: Principal,
    *,
    limit: int = 200,
    include_isolated: bool = False,
) -> dict[str, Any]:
    """读取本人个人空间全景关系图谱（人物、公司、事项与显式关系边）。"""
    if not 1 <= limit <= 500:
        raise InvalidRequestError("limit 必须在 1 到 500 之间")
    tenant, user = await service._workspace.require_employee(who)
    state = await service._store.snapshot(tenant, user)
    matters = await service._workspace.list(who, kind="matter")
    matters_by_id = {m["id"]: m for m in matters}
    return build_full_graph(
        state,
        matters_by_id=matters_by_id,
        limit=limit,
        include_isolated=include_isolated,
    )
