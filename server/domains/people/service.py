"""个人空间「人物与关系」领域服务：本人授权、名片管理、关系图与头像对象。"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.objectstore.base import ObjectNotFoundError, ObjectStore, ObjectTooLargeError
from server.domains.people.models import (
    MAX_AVATAR_BYTES,
    MAX_NOTES,
    MAX_PEOPLE,
    MAX_RELATIONS,
    MergeInput,
    NoteData,
    PeopleBundleInput,
    RelationData,
    RelationPatch,
    UnmergeInput,
    detect_avatar_mime,
    public_note,
    public_person,
    public_relation,
    validate_profile,
)
from server.domains.people.rules import (
    append_audit,
    apply_merge,
    apply_unmerge,
    canonical_relation,
    check_duplicate_relation,
    check_quota,
    check_version,
    expand_graph,
    find_similar_candidates,
    get_note,
    get_person,
    get_relation,
    matches_person,
    new_person_record,
    remember,
    replay,
    request_key,
    restore_person,
    sync_person_profile,
    trash_person,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.core.tenancy import Principal
    from server.domains.people.models import PeopleState
    from server.domains.people.store import PeopleStore
    from server.domains.workspace.service import WorkspaceService


def _validate_model[T](model: type[T], raw: dict[str, Any], label: str) -> T:
    """用严格 Pydantic 模型校验输入，统一转为 400 InvalidRequestError。"""
    try:
        return model.model_validate(raw)  # type: ignore[attr-defined, no-any-return]
    except ValidationError as exc:
        raise InvalidRequestError(f"{label}无效：{exc.errors()[0]['msg']}") from exc


class PeopleService:
    """个人空间人物、关系与互动记录用例，所有读写按 (tenant_id, user_id) 隔离。"""

    def __init__(
        self,
        *,
        store: PeopleStore,
        workspace: WorkspaceService,
        objects: ObjectStore,
        now: Callable[[], float] = time.time,
    ) -> None:
        """注入存储、个人工作台（复用员工身份与事项/文件校验）、对象存储与时钟。"""
        self._store = store
        self._workspace = workspace
        self._objects = objects
        self._now = now

    async def _require_matter(self, who: Principal, matter_id: str) -> dict[str, Any]:
        """校验目标事项属于本人且未移入回收站。"""
        item = await self._workspace.get(who, matter_id)
        if item["kind"] != "matter":
            raise InvalidRequestError("关联目标必须是个人事项")
        if item["trashed_at"] is not None:
            raise ConflictError("目标事项已在回收站，不能新建关联")
        return item

    async def _require_files(self, who: Principal, file_ids: list[str]) -> None:
        """校验附件文件均属于本人且未移入回收站。"""
        for file_id in dict.fromkeys(file_ids):
            item = await self._workspace.get(who, file_id)
            if item["kind"] != "file":
                raise InvalidRequestError("附件引用必须是个人文件")
            if item["trashed_at"] is not None:
                raise ConflictError("附件文件已在回收站，请先恢复")

    async def list_people(
        self,
        who: Principal,
        *,
        q: str | None = None,
        tag: str | None = None,
        organization: str | None = None,
        birth_month: int | None = None,
        matter_id: str | None = None,
        trashed: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """按条件筛选本人的人物列表（排除已合并档案）。"""
        if not 1 <= limit <= 100:
            raise InvalidRequestError("limit 必须在 1 到 100 之间")
        if birth_month is not None and not 1 <= birth_month <= 12:
            raise InvalidRequestError("birth_month 必须在 1 到 12 之间")
        tenant, user = await self._workspace.require_employee(who)
        state = await self._store.snapshot(tenant, user)
        matter_person_ids: set[str] | None = None
        if matter_id is not None:
            matter_person_ids = {
                rel["from_person_id"]
                for rel in state.relations.values()
                if rel["trashed_at"] is None
                and rel["target_kind"] == "matter"
                and rel["target_id"] == matter_id
            }
        matched: list[dict[str, Any]] = []
        for rec in state.persons.values():
            if rec["merged_into_id"] is not None:
                continue
            if (rec["trashed_at"] is not None) != trashed:
                continue
            if matter_person_ids is not None and rec["id"] not in matter_person_ids:
                continue
            if matches_person(rec, q=q, tag=tag, organization=organization, birth_month=birth_month):
                matched.append(rec)
        matched.sort(key=lambda r: (-r["updated_at"], r["id"]))
        return [public_person(rec) for rec in matched[:limit]]

    async def find_candidates(self, who: Principal, profile: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
        """根据姓名、别名或联系方式检索疑似已存在的人物候选。"""
        tenant, user = await self._workspace.require_employee(who)
        state = await self._store.snapshot(tenant, user)
        return find_similar_candidates(state, profile, limit=limit)

    async def get_person(
        self, who: Principal, person_id: str, *, include_history: bool = False
    ) -> dict[str, Any]:
        """读取人物详情，附带关联关系与最近 20 条互动记录。"""
        tenant, user = await self._workspace.require_employee(who)
        state = await self._store.snapshot(tenant, user)
        rec = get_person(state, person_id)
        if rec["merged_into_id"] is not None:
            target = state.persons.get(rec["merged_into_id"])
            return {
                "person": public_person(rec),
                "merged_into": public_person(target) if target is not None else None,
                "relations": [],
                "notes": [],
            }
        relations = [
            public_relation(rel)
            for rel in state.relations.values()
            if rel["trashed_at"] is None
            and (include_history or rel["valid_to"] is None)
            and (
                rel["from_person_id"] == person_id
                or (rel["target_kind"] == "person" and rel["target_id"] == person_id)
            )
        ]
        relations.sort(key=lambda r: (-r["updated_at"], r["id"]))
        notes = [
            public_note(note)
            for note in state.notes.values()
            if note["trashed_at"] is None and person_id in note["person_ids"]
        ]
        notes.sort(key=lambda n: (-n["occurred_at"], n["id"]))
        return {"person": public_person(rec), "relations": relations, "notes": notes[:20]}

    async def create_person(self, who: Principal, profile_raw: dict[str, Any], request_id: str) -> dict[str, Any]:
        """新建本人人物档案，支持幂等重放。"""
        key = request_key(request_id)
        profile = validate_profile(profile_raw)
        tenant, user = await self._workspace.require_employee(who)
        await self._require_files(who, profile["attachment_file_ids"])
        payload = {"op": "create_person", "profile": profile}

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            if (cached := replay(state, key, payload)) is not None:
                return cached
            check_quota(state.persons, MAX_PEOPLE, "人物档案")
            now = self._now()
            rec = new_person_record(profile, now)
            state.persons[rec["id"]] = rec
            append_audit(state, "people.person.created", "person", rec["id"], rec["version"], now)
            result = public_person(rec)
            remember(state, key, payload, result)
            return result

    async def update_person(
        self, who: Principal, person_id: str, profile_raw: dict[str, Any], version: int
    ) -> dict[str, Any]:
        """带版本 CAS 更新人物名片资料。"""
        profile = validate_profile(profile_raw)
        tenant, user = await self._workspace.require_employee(who)
        await self._require_files(who, profile["attachment_file_ids"])

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = get_person(state, person_id, active=True)
            check_version(rec, version)
            now = self._now()
            sync_person_profile(rec, profile, now)
            append_audit(state, "people.person.updated", "person", person_id, rec["version"], now)
            return public_person(rec)

    async def trash_person(self, who: Principal, person_id: str, version: int) -> dict[str, Any]:
        """将人物与关联的活动关系边移入回收站。"""
        tenant, user = await self._workspace.require_employee(who)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = trash_person(state, person_id, version, self._now())
            return public_person(rec)

    async def restore_person(self, who: Principal, person_id: str, version: int) -> dict[str, Any]:
        """从回收站恢复人物及其同批次回收的关系边。"""
        tenant, user = await self._workspace.require_employee(who)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = restore_person(state, person_id, version, self._now())
            return public_person(rec)

    async def merge_people(self, who: Principal, source_id: str, body: MergeInput) -> dict[str, Any]:
        """将源人物软合并到目标人物，支持幂等重放与后续撤销。"""
        key = request_key(body.request_id)
        tenant, user = await self._workspace.require_employee(who)
        merged_dict = body.merged_profile.model_dump() if body.merged_profile is not None else None
        if merged_dict is not None:
            await self._require_files(who, merged_dict["attachment_file_ids"])
        payload = {
            "op": "merge_people",
            "source_id": source_id,
            "target_id": body.target_id,
            "source_version": body.source_version,
            "target_version": body.target_version,
            "merged_profile": merged_dict,
        }

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            if (cached := replay(state, key, payload)) is not None:
                return cached
            source, target = apply_merge(
                state,
                source_id=source_id,
                target_id=body.target_id,
                source_version=body.source_version,
                target_version=body.target_version,
                merged_profile=merged_dict,
                now=self._now(),
            )
            result = {"source": public_person(source), "target": public_person(target)}
            remember(state, key, payload, result)
            return result

    async def unmerge_people(self, who: Principal, source_id: str, body: UnmergeInput) -> dict[str, Any]:
        """撤销一次已执行的软合并。"""
        key = request_key(body.request_id)
        tenant, user = await self._workspace.require_employee(who)
        payload = {
            "op": "unmerge_people",
            "source_id": source_id,
            "source_version": body.source_version,
            "target_version": body.target_version,
        }

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            if (cached := replay(state, key, payload)) is not None:
                return cached
            source, target = apply_unmerge(
                state,
                source_id=source_id,
                source_version=body.source_version,
                target_version=body.target_version,
                now=self._now(),
            )
            result = {"source": public_person(source), "target": public_person(target)}
            remember(state, key, payload, result)
            return result

    async def upload_avatar(self, who: Principal, person_id: str, data: bytes, version: int) -> dict[str, Any]:
        """上传人物头像：先校验位图魔数并写入对象存储，再提交元数据。"""
        mime = detect_avatar_mime(data)
        tenant, user = await self._workspace.require_employee(who)
        # 先检查人物存在且版本匹配，避免无效上传白写对象
        snap = await self._store.snapshot(tenant, user)
        check_version(get_person(snap, person_id, active=True), version)
        object_key = f"{tenant}/people-avatars/{user}/{person_id}/{secrets.token_hex(12)}"
        sha = hashlib.sha256(data).hexdigest()
        await self._objects.put(object_key, data, content_type=mime)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = get_person(state, person_id, active=True)
            check_version(rec, version)
            now = self._now()
            rec["avatar"] = {
                "object_key": object_key,
                "mime_type": mime,
                "size": len(data),
                "sha256": sha,
                "updated_at": now,
            }
            rec["version"] += 1
            rec["updated_at"] = now
            append_audit(state, "people.avatar.updated", "person", person_id, rec["version"], now)
            return public_person(rec)

    async def get_avatar(self, who: Principal, person_id: str) -> tuple[bytes, str]:
        """按本人身份读取人物头像字节与 MIME 类型。"""
        tenant, user = await self._workspace.require_employee(who)
        state = await self._store.snapshot(tenant, user)
        rec = get_person(state, person_id)
        if rec["merged_into_id"] is not None:
            rec = get_person(state, rec["merged_into_id"])
        avatar = rec.get("avatar")
        if avatar is None:
            raise NotFoundError("该人物尚未设置头像")
        try:
            content = await self._objects.get_limited(avatar["object_key"], max_bytes=MAX_AVATAR_BYTES)
        except (ObjectNotFoundError, ObjectTooLargeError) as exc:
            raise NotFoundError("人物头像内容不可用") from exc
        return content, str(avatar["mime_type"])

    async def delete_avatar(self, who: Principal, person_id: str, version: int) -> dict[str, Any]:
        """清除人物头像元数据。"""
        tenant, user = await self._workspace.require_employee(who)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = get_person(state, person_id, active=True)
            check_version(rec, version)
            now = self._now()
            rec["avatar"] = None
            rec["version"] += 1
            rec["updated_at"] = now
            append_audit(state, "people.avatar.deleted", "person", person_id, rec["version"], now)
            return public_person(rec)

    async def create_relation(self, who: Principal, raw: dict[str, Any], request_id: str) -> dict[str, Any]:
        """建立人↔人或人↔个人事项的关系边。"""
        key = request_key(request_id)
        data = _validate_model(RelationData, raw, "关系数据")
        tenant, user = await self._workspace.require_employee(who)
        if data.target_kind == "matter":
            await self._require_matter(who, data.target_id)
        payload = {"op": "create_relation", "data": data.model_dump()}

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            if (cached := replay(state, key, payload)) is not None:
                return cached
            rec = self._insert_relation_in_tx(state, data, self._now())
            result = public_relation(rec)
            remember(state, key, payload, result)
            return result

    def _insert_relation_in_tx(self, state: PeopleState, data: RelationData, now: float) -> dict[str, Any]:
        """在已持锁事务中校验端点、防重并插入关系边。"""
        check_quota(state.relations, MAX_RELATIONS, "关系记录")
        get_person(state, data.from_person_id, active=True)
        if data.target_kind == "person":
            get_person(state, data.target_id, active=True)
        if data.source_note_id is not None:
            get_note(state, data.source_note_id, active=True)
        from_id, target_id, is_sym = canonical_relation(
            data.from_person_id, data.target_kind, data.target_id, data.relation_type, data.symmetric
        )
        check_duplicate_relation(
            state,
            from_id=from_id,
            target_kind=data.target_kind,
            target_id=target_id,
            relation_type=data.relation_type,
            valid_to=data.valid_to,
        )
        rel_id = "rel_" + secrets.token_hex(8)
        rec: dict[str, Any] = {
            "id": rel_id,
            "from_person_id": from_id,
            "target_kind": data.target_kind,
            "target_id": target_id,
            "relation_type": data.relation_type,
            "symmetric": is_sym,
            "role": data.role,
            "note": data.note,
            "valid_from": data.valid_from,
            "valid_to": data.valid_to,
            "source_note_id": data.source_note_id,
            "version": 1,
            "trashed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        state.relations[rel_id] = rec
        append_audit(state, "people.relation.created", "relation", rel_id, 1, now)
        return rec

    async def update_relation(
        self, who: Principal, relation_id: str, raw: dict[str, Any], version: int
    ) -> dict[str, Any]:
        """带版本 CAS 更新关系属性（角色、备注、起止时间等）。"""
        patch = _validate_model(RelationPatch, raw, "关系更新数据")
        tenant, user = await self._workspace.require_employee(who)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = get_relation(state, relation_id, active=True)
            check_version(rec, version)
            if rec["target_kind"] == "matter" and patch.symmetric:
                raise InvalidRequestError("人物与事项的关系不能标记为对称关系")
            from_id, target_id, is_sym = canonical_relation(
                rec["from_person_id"], rec["target_kind"], rec["target_id"], patch.relation_type, patch.symmetric
            )
            check_duplicate_relation(
                state,
                from_id=from_id,
                target_kind=rec["target_kind"],
                target_id=target_id,
                relation_type=patch.relation_type,
                valid_to=patch.valid_to,
                exclude_id=relation_id,
            )
            now = self._now()
            rec.update(
                from_person_id=from_id,
                target_id=target_id,
                relation_type=patch.relation_type,
                symmetric=is_sym,
                role=patch.role,
                note=patch.note,
                valid_from=patch.valid_from,
                valid_to=patch.valid_to,
                version=rec["version"] + 1,
                updated_at=now,
            )
            append_audit(state, "people.relation.updated", "relation", relation_id, rec["version"], now)
            return public_relation(rec)

    async def delete_relation(self, who: Principal, relation_id: str, version: int) -> dict[str, Any]:
        """软删除一条关系边。"""
        tenant, user = await self._workspace.require_employee(who)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = get_relation(state, relation_id, active=True)
            check_version(rec, version)
            now = self._now()
            rec["trashed_at"] = now
            rec["version"] += 1
            rec["updated_at"] = now
            append_audit(state, "people.relation.trashed", "relation", relation_id, rec["version"], now)
            return public_relation(rec)

    async def list_matter_people(self, who: Principal, matter_id: str) -> list[dict[str, Any]]:
        """按本人事项反查所有关联的活动人物与角色。"""
        tenant, user = await self._workspace.require_employee(who)
        await self._workspace.get(who, matter_id)
        state = await self._store.snapshot(tenant, user)
        items: list[dict[str, Any]] = []
        for rel in state.relations.values():
            if rel["trashed_at"] is not None or rel["target_kind"] != "matter" or rel["target_id"] != matter_id:
                continue
            person = state.persons.get(rel["from_person_id"])
            if person is None or person["trashed_at"] is not None or person["merged_into_id"] is not None:
                continue
            items.append({"person": public_person(person), "relation": public_relation(rel)})
        items.sort(key=lambda entry: (-entry["relation"]["updated_at"], entry["person"]["id"]))
        return items

    async def graph(
        self, who: Principal, person_id: str, *, depth: int = 2, include_matters: bool = True
    ) -> dict[str, Any]:
        """展开 1–2 跳人脉与事项关系图。"""
        tenant, user = await self._workspace.require_employee(who)
        state = await self._store.snapshot(tenant, user)
        return expand_graph(state, person_id, depth=depth, include_matters=include_matters)

    async def list_notes(
        self,
        who: Principal,
        *,
        person_id: str | None = None,
        matter_id: str | None = None,
        q: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """检索本人原始互动记录。"""
        if not 1 <= limit <= 100:
            raise InvalidRequestError("limit 必须在 1 到 100 之间")
        tenant, user = await self._workspace.require_employee(who)
        state = await self._store.snapshot(tenant, user)
        needle = q.strip().casefold() if q else ""
        items: list[dict[str, Any]] = []
        for note in state.notes.values():
            if note["trashed_at"] is not None:
                continue
            if person_id is not None and person_id not in note["person_ids"]:
                continue
            if matter_id is not None and note["matter_id"] != matter_id:
                continue
            if needle and needle not in note["content"].casefold():
                continue
            items.append(note)
        items.sort(key=lambda n: (-n["occurred_at"], n["id"]))
        return [public_note(n) for n in items[:limit]]

    async def create_note(self, who: Principal, raw: dict[str, Any], request_id: str) -> dict[str, Any]:
        """保存一条原始互动、名片文本或会面纪要。"""
        key = request_key(request_id)
        data = _validate_model(NoteData, raw, "互动记录")
        tenant, user = await self._workspace.require_employee(who)
        if data.matter_id is not None:
            await self._require_matter(who, data.matter_id)
        await self._require_files(who, data.attachment_file_ids)
        payload = {"op": "create_note", "data": data.model_dump()}

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            if (cached := replay(state, key, payload)) is not None:
                return cached
            rec = self._insert_note_in_tx(state, data, self._now())
            result = public_note(rec)
            remember(state, key, payload, result)
            return result

    def _insert_note_in_tx(self, state: PeopleState, data: NoteData, now: float) -> dict[str, Any]:
        """在已持锁事务中校验关联人物并插入原始记录。"""
        check_quota(state.notes, MAX_NOTES, "互动记录")
        dedup_persons = list(dict.fromkeys(data.person_ids))
        for pid in dedup_persons:
            get_person(state, pid, active=True)
        note_id = "pnote_" + secrets.token_hex(8)
        rec: dict[str, Any] = {
            "id": note_id,
            "occurred_at": data.occurred_at if data.occurred_at is not None else now,
            "content": data.content,
            "person_ids": dedup_persons,
            "matter_id": data.matter_id,
            "attachment_file_ids": list(dict.fromkeys(data.attachment_file_ids)),
            "source_message_id": data.source_message_id,
            "version": 1,
            "trashed_at": None,
            "created_at": now,
            "updated_at": now,
        }
        state.notes[note_id] = rec
        append_audit(state, "people.note.created", "note", note_id, 1, now)
        return rec

    async def delete_note(self, who: Principal, note_id: str, version: int) -> dict[str, Any]:
        """软删除一条原始互动记录。"""
        tenant, user = await self._workspace.require_employee(who)

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            rec = get_note(state, note_id, active=True)
            check_version(rec, version)
            now = self._now()
            rec["trashed_at"] = now
            rec["version"] += 1
            rec["updated_at"] = now
            append_audit(state, "people.note.trashed", "note", note_id, rec["version"], now)
            return public_note(rec)

    async def apply_bundle(
        self, who: Principal, raw: dict[str, Any], request_id: str
    ) -> dict[str, Any]:
        """在单事务内原子提交复合人物确认卡（人物新建/更新 + 关系边 + 原始记录）。"""
        key = request_key(request_id)
        bundle = _validate_model(PeopleBundleInput, raw, "人物确认包")
        tenant, user = await self._workspace.require_employee(who)
        await self._validate_bundle_external_refs(who, bundle)
        payload = {"op": "apply_bundle", "bundle": bundle.model_dump()}

        async def guard() -> None:
            await self._workspace.require_employee(who)

        async with self._store.transaction(tenant, user, guard) as state:
            if (cached := replay(state, key, payload)) is not None:
                return cached
            now = self._now()
            ref_map: dict[str, str] = {}
            saved_persons: list[dict[str, Any]] = []
            for item in bundle.persons:
                profile = item.profile.model_dump()
                if item.action == "create":
                    check_quota(state.persons, MAX_PEOPLE, "人物档案")
                    rec = new_person_record(profile, now)
                    state.persons[rec["id"]] = rec
                    append_audit(state, "people.person.created", "person", rec["id"], 1, now)
                    if item.temp_ref is not None:
                        ref_map[item.temp_ref] = rec["id"]
                else:
                    assert item.person_id is not None
                    rec = get_person(state, item.person_id, active=True)
                    if item.expected_version is not None:
                        check_version(rec, item.expected_version)
                    sync_person_profile(rec, profile, now)
                    append_audit(state, "people.person.updated", "person", rec["id"], rec["version"], now)
                    if item.temp_ref is not None:
                        ref_map[item.temp_ref] = rec["id"]
                saved_persons.append(public_person(rec))

            saved_note: dict[str, Any] | None = None
            note_id: str | None = None
            if bundle.note is not None:
                resolved_pids = [ref_map.get(ref, ref) for ref in bundle.note.person_refs]
                note_data = NoteData(
                    content=bundle.note.content,
                    occurred_at=bundle.note.occurred_at,
                    person_ids=resolved_pids,
                    matter_id=bundle.note.matter_id,
                    attachment_file_ids=bundle.note.attachment_file_ids,
                    source_message_id=bundle.note.source_message_id,
                )
                note_rec = self._insert_note_in_tx(state, note_data, now)
                note_id = note_rec["id"]
                saved_note = public_note(note_rec)

            saved_relations: list[dict[str, Any]] = []
            for rel in bundle.relations:
                from_id = ref_map.get(rel.from_ref, rel.from_ref)
                target_id = ref_map.get(rel.target_ref, rel.target_ref) if rel.target_kind == "person" else rel.target_ref
                rel_data = RelationData(
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
                rel_rec = self._insert_relation_in_tx(state, rel_data, now)
                saved_relations.append(public_relation(rel_rec))

            result = {
                "persons": saved_persons,
                "relations": saved_relations,
                "note": saved_note,
            }
            remember(state, key, payload, result)
            return result

    async def _validate_bundle_external_refs(self, who: Principal, bundle: PeopleBundleInput) -> None:
        """提前校验复合包里引用的本人事项与附件文件。"""
        matter_ids: set[str] = set()
        file_ids: list[str] = []
        for item in bundle.persons:
            file_ids.extend(item.profile.attachment_file_ids)
        for rel in bundle.relations:
            if rel.target_kind == "matter":
                matter_ids.add(rel.target_ref)
        if bundle.note is not None:
            if bundle.note.matter_id is not None:
                matter_ids.add(bundle.note.matter_id)
            file_ids.extend(bundle.note.attachment_file_ids)
        for mid in matter_ids:
            await self._require_matter(who, mid)
        await self._require_files(who, file_ids)
