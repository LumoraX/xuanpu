"""个人知识图谱复合提交引擎：一次原子/幂等落库原始记录、公司、人物、项目、行动项与跨实体关系。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.domains.people.graph_rules import (
    MAX_ORGS,
    canonical_entity_relation,
    get_org,
    new_org_record,
    sync_org_profile,
)
from server.domains.people.models import (
    MAX_PEOPLE,
    NoteData,
    PeopleBundleInput,
    PeopleState,
    RelationData,
    public_note,
    public_org,
    public_person,
    public_relation,
)
from server.domains.people.rules import (
    append_audit,
    check_quota,
    check_version,
    get_person,
    new_person_record,
    remember,
    replay,
    request_key,
    sync_person_profile,
)
from server.domains.reminders.models import ReminderInput

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.domains.people.service import PeopleService


async def _validate_external_refs(
    service: PeopleService, who: Principal, bundle: PeopleBundleInput
) -> None:
    """提前校验复合包里引用的既有个人事项与附件文件。"""
    local_matter_refs = {m.temp_ref for m in bundle.matters if m.temp_ref is not None}
    matter_ids: set[str] = {m.matter_id for m in bundle.matters if m.matter_id is not None}
    file_ids: list[str] = []
    for item in bundle.persons:
        file_ids.extend(item.profile.attachment_file_ids)
    for rel in bundle.relations:
        if rel.from_kind == "matter" and rel.from_ref not in local_matter_refs:
            matter_ids.add(rel.from_ref)
        if rel.target_kind == "matter" and rel.target_ref not in local_matter_refs:
            matter_ids.add(rel.target_ref)
    for todo in bundle.todos:
        if todo.matter_ref is not None and todo.matter_ref not in local_matter_refs:
            matter_ids.add(todo.matter_ref)
    for ev in bundle.events:
        if ev.matter_ref is not None and ev.matter_ref not in local_matter_refs:
            matter_ids.add(ev.matter_ref)
    if bundle.record is not None:
        if bundle.record.matter_id is not None and bundle.record.matter_id not in local_matter_refs:
            matter_ids.add(bundle.record.matter_id)
        for mref in bundle.record.matter_refs:
            if mref not in local_matter_refs:
                matter_ids.add(mref)
        file_ids.extend(bundle.record.attachment_file_ids)
    for mid in matter_ids:
        await service._require_matter(who, mid)
    await service._require_files(who, file_ids)


async def _materialize_external_items(
    service: PeopleService, who: Principal, bundle: PeopleBundleInput, request_id: str
) -> dict[str, Any]:
    """在图谱事务前幂等创建或校验个人事项、待办、日程与提醒。"""
    matter_ref_map: dict[str, str] = {}
    saved_matters: list[dict[str, Any]] = []
    for idx, item in enumerate(bundle.matters):
        if item.action == "create":
            matter = await service._workspace.create(
                who,
                "matter",
                {"title": item.title, "description": item.description},
                f"{request_id}:m{idx}",
            )
        else:
            assert item.matter_id is not None
            matter = await service._workspace.get(who, item.matter_id)
            if matter["kind"] != "matter" or matter.get("trashed_at") is not None:
                raise NotFoundError("关联的个人事项不存在或已进回收站")
        if item.temp_ref is not None:
            matter_ref_map[item.temp_ref] = matter["id"]
        saved_matters.append(matter)

    saved_todos: list[dict[str, Any]] = []
    for idx, todo in enumerate(bundle.todos):
        mid = matter_ref_map.get(todo.matter_ref, todo.matter_ref) if todo.matter_ref else None
        rec = await service._workspace.create(
            who,
            "todo",
            {"title": todo.title, "description": todo.description, "due_at": todo.due_at, "matter_id": mid},
            f"{request_id}:t{idx}",
        )
        saved_todos.append(rec)

    saved_events: list[dict[str, Any]] = []
    for idx, ev in enumerate(bundle.events):
        mid = matter_ref_map.get(ev.matter_ref, ev.matter_ref) if ev.matter_ref else None
        rec = await service._workspace.create(
            who,
            "event",
            {
                "title": ev.title,
                "description": ev.description,
                "start_at": ev.start_at,
                "end_at": ev.end_at,
                "timezone": ev.timezone,
                "matter_id": mid,
            },
            f"{request_id}:e{idx}",
        )
        saved_events.append(rec)

    saved_reminders: list[dict[str, Any]] = []
    if bundle.reminders:
        if service._reminders is None:
            raise InvalidRequestError("提醒服务当前不可用")
        for idx, rem in enumerate(bundle.reminders):
            full_title = f"{rem.title}（{rem.body}）"[:500] if rem.body.strip() else rem.title
            r_in = ReminderInput(
                title=full_title,
                due_at=rem.due_at,
                timezone=rem.timezone,
            )
            created = await service._reminders.create(who, r_in, f"{request_id}:r{idx}")
            saved_reminders.append(
                {"reminder_id": created.reminder_id, "title": created.title, "due_at": created.due_at}
            )

    return {
        "matter_ref_map": matter_ref_map,
        "matters": saved_matters,
        "todos": saved_todos,
        "events": saved_events,
        "reminders": saved_reminders,
    }


def _resolve_ref(
    kind: str,
    ref: str,
    person_map: dict[str, str],
    org_map: dict[str, str],
    matter_map: dict[str, str],
) -> str:
    """按实体类型把 temp_ref 解析为真实实体 ID。"""
    if kind == "person":
        return person_map.get(ref, ref)
    if kind == "org":
        return org_map.get(ref, ref)
    return matter_map.get(ref, ref)


def _insert_or_find_relation(
    service: PeopleService, state: PeopleState, data: RelationData, now: float
) -> dict[str, Any]:
    """复合包写关系时，若相同有效关系已存在则直接复用，不打断整包保存。"""
    try:
        return service._insert_relation_in_tx(state, data, now)
    except ConflictError:
        fk, fid, tk, tid, _ = canonical_entity_relation(
            data.from_kind, data.from_person_id, data.target_kind, data.target_id, data.relation_type, data.symmetric
        )
        key = (fk, fid, tk, tid, data.relation_type.casefold())
        for rel in state.relations.values():
            if rel["trashed_at"] is not None or rel["valid_to"] is not None:
                continue
            existing = (
                rel.get("from_kind", "person"),
                rel["from_person_id"],
                rel["target_kind"],
                rel["target_id"],
                rel["relation_type"].casefold(),
            )
            if existing == key:
                return rel
        raise


def _apply_orgs_and_persons(
    state: PeopleState, bundle: PeopleBundleInput, now: float
) -> tuple[dict[str, str], list[dict[str, Any]], dict[str, str], list[dict[str, Any]]]:
    """在持锁事务内落库本批全部公司与人物实体。"""
    org_map: dict[str, str] = {}
    saved_orgs: list[dict[str, Any]] = []
    for item in bundle.orgs:
        profile = item.profile.model_dump()
        if item.action == "create":
            check_quota(state.orgs, MAX_ORGS, "公司/组织档案")
            rec = new_org_record(profile, now)
            state.orgs[rec["id"]] = rec
            append_audit(state, "people.org.created", "org", rec["id"], 1, now)
        elif item.action == "update":
            assert item.org_id is not None
            rec = get_org(state, item.org_id, active=True)
            if item.expected_version is not None:
                check_version(rec, item.expected_version)
            sync_org_profile(rec, profile, now)
            append_audit(state, "people.org.updated", "org", rec["id"], rec["version"], now)
        else:
            assert item.org_id is not None
            rec = get_org(state, item.org_id, active=True)
        if item.temp_ref is not None:
            org_map[item.temp_ref] = rec["id"]
        saved_orgs.append(public_org(rec))

    person_map: dict[str, str] = {}
    saved_persons: list[dict[str, Any]] = []
    for p_item in bundle.persons:
        profile = p_item.profile.model_dump()
        if p_item.action == "create":
            check_quota(state.persons, MAX_PEOPLE, "人物档案")
            rec = new_person_record(profile, now)
            state.persons[rec["id"]] = rec
            append_audit(state, "people.person.created", "person", rec["id"], 1, now)
        else:
            assert p_item.person_id is not None
            rec = get_person(state, p_item.person_id, active=True)
            if p_item.expected_version is not None:
                check_version(rec, p_item.expected_version)
            sync_person_profile(rec, profile, now)
            append_audit(state, "people.person.updated", "person", rec["id"], rec["version"], now)
        if p_item.temp_ref is not None:
            person_map[p_item.temp_ref] = rec["id"]
        saved_persons.append(public_person(rec))

    return org_map, saved_orgs, person_map, saved_persons


async def apply_people_bundle(
    service: PeopleService, who: Principal, raw: dict[str, Any], request_id: str
) -> dict[str, Any]:
    """幂等执行个人知识图谱复合包：原始记录 + 公司 + 人物 + 事项 + 待办/日程/提醒 + 关系网。"""
    key = request_key(request_id)
    bundle = service._validate_bundle(raw)
    tenant, user = await service._workspace.require_employee(who)
    await _validate_external_refs(service, who, bundle)
    ext = await _materialize_external_items(service, who, bundle, request_id)
    matter_map: dict[str, str] = ext["matter_ref_map"]
    payload = {"op": "apply_bundle", "bundle": bundle.model_dump()}

    async def guard() -> None:
        await service._workspace.require_employee(who)

    async with service._store.transaction(tenant, user, guard) as state:
        if (cached := replay(state, key, payload)) is not None:
            return cached
        now = service._now()
        org_map, saved_orgs, person_map, saved_persons = _apply_orgs_and_persons(state, bundle, now)

        saved_note: dict[str, Any] | None = None
        note_id: str | None = None
        if bundle.record is not None:
            rec_in = bundle.record
            resolved_pids = list(
                dict.fromkeys([*(person_map.get(r, r) for r in rec_in.person_refs), *(p["id"] for p in saved_persons)])
            )
            resolved_oids = list(
                dict.fromkeys([*(org_map.get(r, r) for r in rec_in.org_refs), *(o["id"] for o in saved_orgs)])
            )
            explicit_mids = [matter_map.get(r, r) for r in rec_in.matter_refs]
            primary_mid = matter_map.get(rec_in.matter_id, rec_in.matter_id) if rec_in.matter_id else None
            resolved_mids = list(
                dict.fromkeys(
                    [*(x for x in (primary_mid,) if x), *explicit_mids, *(m["id"] for m in ext["matters"])]
                )
            )
            note_data = NoteData(
                record_kind=rec_in.record_kind,
                title=rec_in.title,
                location=rec_in.location,
                content=rec_in.content,
                occurred_at=rec_in.occurred_at,
                end_at=rec_in.end_at,
                summary=rec_in.summary,
                decisions=rec_in.decisions,
                open_questions=rec_in.open_questions,
                person_ids=resolved_pids,
                org_ids=resolved_oids,
                matter_id=primary_mid or (resolved_mids[0] if resolved_mids else None),
                matter_ids=resolved_mids,
                todo_ids=[t["id"] for t in ext["todos"]],
                event_ids=[e["id"] for e in ext["events"]],
                reminder_ids=[r["reminder_id"] for r in ext["reminders"]],
                attachment_file_ids=rec_in.attachment_file_ids,
                source_message_id=rec_in.source_message_id,
            )
            note_rec = service._insert_note_in_tx(state, note_data, now)
            note_id = note_rec["id"]
            saved_note = public_note(note_rec)

        saved_relations: list[dict[str, Any]] = []
        for rel in bundle.relations:
            from_id = _resolve_ref(rel.from_kind, rel.from_ref, person_map, org_map, matter_map)
            target_id = _resolve_ref(rel.target_kind, rel.target_ref, person_map, org_map, matter_map)
            rel_data = RelationData(
                from_kind=rel.from_kind,
                from_person_id=from_id,
                target_kind=rel.target_kind,
                target_id=target_id,
                relation_type=rel.relation_type,
                symmetric=rel.symmetric,
                role=rel.role,
                note=rel.note,
                valid_from=rel.valid_from,
                valid_to=rel.valid_to,
                source_note_id=note_id,
            )
            rel_rec = _insert_or_find_relation(service, state, rel_data, now)
            saved_relations.append(public_relation(rel_rec))

        result = {
            "record": saved_note,
            "note": saved_note,
            "orgs": saved_orgs,
            "persons": saved_persons,
            "matters": ext["matters"],
            "todos": ext["todos"],
            "events": ext["events"],
            "reminders": ext["reminders"],
            "relations": saved_relations,
        }
        remember(state, key, payload, result)
        return result
