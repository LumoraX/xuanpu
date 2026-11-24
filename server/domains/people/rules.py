"""人物与关系领域的纯内存不变量：合并、关系防重、检索与多跳展开。"""

from __future__ import annotations

import secrets
from collections import deque
from copy import deepcopy
from typing import Any

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.domains.people.models import (
    MAX_PEOPLE,
    SYMMETRIC_TYPES,
    PeopleState,
    fingerprint,
    public_person,
    public_relation,
)


def request_key(value: str) -> str:
    """校验显式幂等键。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidRequestError("request_id 必须为 1 至 128 字的非空字符串")
    return value


def replay(state: PeopleState, request_id: str, payload: Any) -> dict[str, Any] | None:
    """同幂等键同参数返回原结果，异参数返回 409 冲突。"""
    saved = state.requests.get(request_id)
    if saved is None:
        return None
    if saved["fingerprint"] != fingerprint(payload):
        raise ConflictError("request_id 已用于其他操作或内容")
    return dict(deepcopy(saved["result"]))


def remember(state: PeopleState, request_id: str, payload: Any, result: dict[str, Any]) -> None:
    """保存本次操作的幂等回执。"""
    state.requests[request_id] = {
        "fingerprint": fingerprint(payload),
        "result": deepcopy(result),
    }


def append_audit(
    state: PeopleState, action: str, entity_kind: str, entity_id: str, version: int, now: float
) -> None:
    """仅记录脱敏审计元数据，严禁写入姓名、联系方式、生日或正文。"""
    state.audit.append({
        "kind": action,
        "at": now,
        "data": {"entity_kind": entity_kind, "entity_id": entity_id, "version": version},
    })


def check_version(record: dict[str, Any], expected_version: int) -> None:
    """所有状态变更操作均执行乐观锁版本校验。"""
    if type(expected_version) is not int or expected_version < 1:
        raise InvalidRequestError("version 必须为正整数")
    if record["version"] != expected_version:
        raise ConflictError("记录版本已变化，请刷新后重试")


def get_person(state: PeopleState, person_id: str, *, active: bool = False) -> dict[str, Any]:
    """查找本人人物记录，可要求未回收且未被合并。"""
    record = state.persons.get(person_id)
    if record is None:
        raise NotFoundError("人物记录不存在")
    if active and record["trashed_at"] is not None:
        raise ConflictError("人物记录已在回收站，请先恢复")
    if active and record["merged_into_id"] is not None:
        raise ConflictError("人物记录已合并到其他档案，请操作目标档案")
    return record


def get_relation(state: PeopleState, relation_id: str, *, active: bool = False) -> dict[str, Any]:
    """查找本人关系边。"""
    record = state.relations.get(relation_id)
    if record is None:
        raise NotFoundError("关系记录不存在")
    if active and record["trashed_at"] is not None:
        raise ConflictError("关系记录已删除")
    return record


def get_note(state: PeopleState, note_id: str, *, active: bool = False) -> dict[str, Any]:
    """查找本人原始互动记录。"""
    record = state.notes.get(note_id)
    if record is None:
        raise NotFoundError("互动记录不存在")
    if active and record["trashed_at"] is not None:
        raise ConflictError("互动记录已删除")
    return record


def check_quota(collection: dict[str, dict[str, Any]], limit: int, label: str) -> None:
    """限制单员工活动记录数，防止无界膨胀。"""
    active_count = sum(
        1
        for item in collection.values()
        if item["trashed_at"] is None and item.get("merged_into_id") is None
    )
    if active_count >= limit:
        raise ConflictError(f"{label}数量已达上限（{limit}）")


def new_person_record(profile: dict[str, Any], now: float) -> dict[str, Any]:
    """构造新的人物记录并派生常用检索冗余字段。"""
    birth = profile.get("birth") or {}
    return {
        "id": "per_" + secrets.token_hex(8),
        "name": profile["name"],
        "organization": profile.get("organization", ""),
        "birth_year": birth.get("year"),
        "birth_month": birth.get("month"),
        "birth_day": birth.get("day"),
        "profile": deepcopy(profile),
        "avatar": None,
        "version": 1,
        "merged_into_id": None,
        "merge_snapshot": None,
        "trashed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def sync_person_profile(record: dict[str, Any], profile: dict[str, Any], now: float) -> None:
    """更新人物 profile 时同步派生检索列与版本号。"""
    birth = profile.get("birth") or {}
    record["name"] = profile["name"]
    record["organization"] = profile.get("organization", "")
    record["birth_year"] = birth.get("year")
    record["birth_month"] = birth.get("month")
    record["birth_day"] = birth.get("day")
    record["profile"] = deepcopy(profile)
    record["version"] += 1
    record["updated_at"] = now


def canonical_relation(
    from_id: str, target_kind: str, target_id: str, relation_type: str, symmetric: bool
) -> tuple[str, str, bool]:
    """规范化对称人际关系的端点顺序，避免同一对人存两条重复边。"""
    is_sym = bool(symmetric or (target_kind == "person" and relation_type.casefold() in SYMMETRIC_TYPES))
    if target_kind == "matter":
        return from_id, target_id, False
    if is_sym and from_id > target_id:
        return target_id, from_id, True
    return from_id, target_id, is_sym


def check_duplicate_relation(
    state: PeopleState,
    *,
    from_id: str,
    target_kind: str,
    target_id: str,
    relation_type: str,
    valid_to: str | None,
    exclude_id: str | None = None,
) -> None:
    """阻止同一对实体间建立重复的当前有效关系。"""
    if valid_to is not None:
        return
    key = (from_id, target_kind, target_id, relation_type.casefold())
    for rel in state.relations.values():
        if rel["id"] == exclude_id or rel["trashed_at"] is not None or rel["valid_to"] is not None:
            continue
        existing = (rel["from_person_id"], rel["target_kind"], rel["target_id"], rel["relation_type"].casefold())
        if existing == key:
            raise ConflictError("已存在相同的有效关系记录，请直接更新既有关系")


def _merge_unique_strings(left: list[str], right: list[str], limit: int) -> list[str]:
    """按保序不区分大小写合并字符串列表。"""
    seen: set[str] = set()
    merged: list[str] = []
    for item in (*left, *right):
        marker = item.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def _merge_contacts(left: list[dict[str, Any]], right: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 (kind, value) 去重合并联系方式列表。"""
    seen: set[tuple[str, str]] = set()
    merged: list[dict[str, Any]] = []
    for entry in (*left, *right):
        key = (entry["kind"], entry["value"].strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        merged.append(deepcopy(entry))
        if len(merged) >= 20:
            break
    return merged


def build_merged_profile(source: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """当调用方未显式给出 merged_profile 时，按目标优先、集合求并策略自动合成。"""
    sp, tp = source["profile"], target["profile"]
    merged: dict[str, Any] = deepcopy(tp)
    extra_aliases = [sp["name"], *sp.get("aliases", [])]
    aliases = [a for a in extra_aliases if a.casefold() != tp["name"].casefold()]
    merged["aliases"] = _merge_unique_strings(tp.get("aliases", []), aliases, 20)
    merged["contacts"] = _merge_contacts(tp.get("contacts", []), sp.get("contacts", []))
    merged["tags"] = _merge_unique_strings(tp.get("tags", []), sp.get("tags", []), 30)
    merged["attachment_file_ids"] = _merge_unique_strings(
        tp.get("attachment_file_ids", []), sp.get("attachment_file_ids", []), 10
    )
    for field_name in ("organization", "department", "title", "city", "summary"):
        if not merged.get(field_name) and sp.get(field_name):
            merged[field_name] = sp[field_name]
    if merged.get("gender") == "unknown" and sp.get("gender", "unknown") != "unknown":
        merged["gender"] = sp["gender"]
    if merged.get("birth") is None and sp.get("birth") is not None:
        merged["birth"] = deepcopy(sp["birth"])
    extra = dict(sp.get("extra", {}))
    extra.update(tp.get("extra", {}))
    merged["extra"] = dict(list(extra.items())[:30])
    return merged


def _rewrite_relation_for_merge(
    state: PeopleState, rel: dict[str, Any], source_id: str, target_id: str, now: float
) -> dict[str, Any] | None:
    """将归属源人物的活动关系迁到目标人物；形成自环或重复边则软归并。"""
    if rel["trashed_at"] is not None:
        return None
    touches_from = rel["from_person_id"] == source_id
    touches_target = rel["target_kind"] == "person" and rel["target_id"] == source_id
    if not touches_from and not touches_target:
        return None
    before = {
        "relation_id": rel["id"],
        "from_person_id": rel["from_person_id"],
        "target_id": rel["target_id"],
        "trashed_at": rel["trashed_at"],
    }
    new_from = target_id if touches_from else rel["from_person_id"]
    new_target = target_id if touches_target else rel["target_id"]
    if rel["target_kind"] == "person" and new_from == new_target:
        rel["trashed_at"] = now
        rel["version"] += 1
        rel["updated_at"] = now
        return before
    new_from, new_target, is_sym = canonical_relation(
        new_from, rel["target_kind"], new_target, rel["relation_type"], rel["symmetric"]
    )
    try:
        check_duplicate_relation(
            state,
            from_id=new_from,
            target_kind=rel["target_kind"],
            target_id=new_target,
            relation_type=rel["relation_type"],
            valid_to=rel["valid_to"],
            exclude_id=rel["id"],
        )
    except ConflictError:
        rel["trashed_at"] = now
        rel["version"] += 1
        rel["updated_at"] = now
        return before
    rel["from_person_id"] = new_from
    rel["target_id"] = new_target
    rel["symmetric"] = is_sym
    rel["version"] += 1
    rel["updated_at"] = now
    return before


def apply_merge(
    state: PeopleState,
    *,
    source_id: str,
    target_id: str,
    source_version: int,
    target_version: int,
    merged_profile: dict[str, Any] | None,
    now: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """将源人物软合并到目标人物，保存可逆快照并迁移关系与笔记。"""
    if source_id == target_id:
        raise InvalidRequestError("不能将人物合并到自身")
    source = get_person(state, source_id, active=True)
    target = get_person(state, target_id, active=True)
    check_version(source, source_version)
    check_version(target, target_version)
    if any(p.get("merged_into_id") == source_id for p in state.persons.values()):
        raise ConflictError("该人物已接收过其他合并档案，请先撤销或直接保留其为主档案")

    final_profile = merged_profile if merged_profile is not None else build_merged_profile(source, target)
    inherited_avatar = False
    target_avatar_before = deepcopy(target["avatar"])
    if target["avatar"] is None and source["avatar"] is not None:
        target["avatar"] = deepcopy(source["avatar"])
        inherited_avatar = True

    rel_changes: list[dict[str, Any]] = []
    for rel in state.relations.values():
        changed = _rewrite_relation_for_merge(state, rel, source_id, target_id, now)
        if changed is not None:
            rel_changes.append(changed)

    note_changes: list[dict[str, Any]] = []
    for note in state.notes.values():
        if note["trashed_at"] is None and source_id in note["person_ids"]:
            note_changes.append({"note_id": note["id"], "person_ids": list(note["person_ids"])})
            updated_ids = [target_id if pid == source_id else pid for pid in note["person_ids"]]
            note["person_ids"] = list(dict.fromkeys(updated_ids))
            note["version"] += 1
            note["updated_at"] = now

    source["merge_snapshot"] = {
        "target_id": target_id,
        "target_profile_before": deepcopy(target["profile"]),
        "target_avatar_before": target_avatar_before,
        "inherited_avatar": inherited_avatar,
        "relations": rel_changes,
        "notes": note_changes,
    }
    source["merged_into_id"] = target_id
    source["version"] += 1
    source["updated_at"] = now
    sync_person_profile(target, final_profile, now)
    append_audit(state, "people.person.merged", "person", source_id, source["version"], now)
    append_audit(state, "people.person.updated", "person", target_id, target["version"], now)
    return source, target


def apply_unmerge(
    state: PeopleState, *, source_id: str, source_version: int, target_version: int, now: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    """根据合并快照撤销一次软合并，恢复独立档案与原关系归属。"""
    source = get_person(state, source_id)
    target_id = source.get("merged_into_id")
    snapshot = source.get("merge_snapshot")
    if target_id is None or snapshot is None:
        raise ConflictError("该人物未处于已合并状态")
    target = get_person(state, target_id, active=True)
    check_version(source, source_version)
    check_version(target, target_version)

    for entry in snapshot["relations"]:
        rel = state.relations.get(entry["relation_id"])
        if rel is None:
            continue
        rel["from_person_id"] = entry["from_person_id"]
        rel["target_id"] = entry["target_id"]
        rel["trashed_at"] = entry["trashed_at"]
        rel["version"] += 1
        rel["updated_at"] = now

    for entry in snapshot["notes"]:
        note = state.notes.get(entry["note_id"])
        if note is None:
            continue
        note["person_ids"] = list(entry["person_ids"])
        note["version"] += 1
        note["updated_at"] = now

    if snapshot.get("inherited_avatar"):
        target["avatar"] = deepcopy(snapshot.get("target_avatar_before"))
    sync_person_profile(target, snapshot["target_profile_before"], now)
    source["merged_into_id"] = None
    source["merge_snapshot"] = None
    source["version"] += 1
    source["updated_at"] = now
    append_audit(state, "people.person.unmerged", "person", source_id, source["version"], now)
    append_audit(state, "people.person.updated", "person", target_id, target["version"], now)
    return source, target


def trash_person(state: PeopleState, person_id: str, version: int, now: float) -> dict[str, Any]:
    """将人物与附着其上的活动关系按同一批次移入回收站。"""
    person = get_person(state, person_id, active=True)
    check_version(person, version)
    batch = "tr_" + secrets.token_hex(6)
    person["trashed_at"] = now
    person["_trash_batch"] = batch
    person["version"] += 1
    person["updated_at"] = now
    for rel in state.relations.values():
        if rel["trashed_at"] is None and (
            rel["from_person_id"] == person_id
            or (rel["target_kind"] == "person" and rel["target_id"] == person_id)
        ):
            rel["trashed_at"] = now
            rel["_trash_batch"] = batch
            rel["version"] += 1
            rel["updated_at"] = now
    append_audit(state, "people.person.trashed", "person", person_id, person["version"], now)
    return person


def restore_person(state: PeopleState, person_id: str, version: int, now: float) -> dict[str, Any]:
    """从回收站恢复人物及其同批次回收的有效关系边。"""
    person = get_person(state, person_id)
    if person["trashed_at"] is None:
        raise ConflictError("人物记录不在回收站")
    check_version(person, version)
    check_quota(state.persons, MAX_PEOPLE, "人物档案")
    batch = person.pop("_trash_batch", None)
    person["trashed_at"] = None
    person["version"] += 1
    person["updated_at"] = now
    if batch is not None:
        for rel in state.relations.values():
            if rel.get("_trash_batch") != batch:
                continue
            other_ok = True
            if rel["target_kind"] == "person":
                other_id = rel["target_id"] if rel["from_person_id"] == person_id else rel["from_person_id"]
                other = state.persons.get(other_id)
                other_ok = other is not None and other["trashed_at"] is None and other["merged_into_id"] is None
            if other_ok:
                rel.pop("_trash_batch", None)
                rel["trashed_at"] = None
                rel["version"] += 1
                rel["updated_at"] = now
    append_audit(state, "people.person.restored", "person", person_id, person["version"], now)
    return person


def _extra_searchable_text(extra: dict[str, Any]) -> list[str]:
    """提取扩展字段中的可检索文本片段。"""
    parts: list[str] = []
    for key, val in extra.items():
        parts.append(str(key))
        if isinstance(val, list):
            parts.extend(str(item) for item in val)
        else:
            parts.append(str(val))
    return parts


def matches_person(
    record: dict[str, Any],
    *,
    q: str | None = None,
    tag: str | None = None,
    organization: str | None = None,
    birth_month: int | None = None,
) -> bool:
    """不区分大小写的字面筛选，覆盖姓名、别名、公司、标签、联系方式与扩展字段。"""
    profile = record["profile"]
    if birth_month is not None and record.get("birth_month") != birth_month:
        return False
    if organization:
        if organization.casefold() not in profile.get("organization", "").casefold():
            return False
    if tag:
        if not any(t.casefold() == tag.casefold() for t in profile.get("tags", [])):
            return False
    if q:
        needle = q.strip().casefold()
        if not needle:
            return True
        haystack = [
            profile["name"],
            *profile.get("aliases", []),
            profile.get("organization", ""),
            profile.get("department", ""),
            profile.get("title", ""),
            profile.get("city", ""),
            profile.get("summary", ""),
            *profile.get("tags", []),
            *(c["value"] for c in profile.get("contacts", [])),
            *_extra_searchable_text(profile.get("extra", {})),
        ]
        return any(needle in text.casefold() for text in haystack if text)
    return True


def find_similar_candidates(state: PeopleState, profile: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """按姓名、别名或联系方式查找疑似已存在的人物候选，供确认卡消歧提示。"""
    names = {profile["name"].casefold(), *(a.casefold() for a in profile.get("aliases", []))}
    contacts = {c["value"].strip().casefold() for c in profile.get("contacts", [])}
    hits: list[dict[str, Any]] = []
    for rec in state.persons.values():
        if rec["trashed_at"] is not None or rec["merged_into_id"] is not None:
            continue
        rec_profile = rec["profile"]
        rec_names = {rec_profile["name"].casefold(), *(a.casefold() for a in rec_profile.get("aliases", []))}
        rec_contacts = {c["value"].strip().casefold() for c in rec_profile.get("contacts", [])}
        if (names & rec_names) or (contacts and (contacts & rec_contacts)):
            hits.append({
                "person_id": rec["id"],
                "name": rec_profile["name"],
                "organization": rec_profile.get("organization", ""),
                "title": rec_profile.get("title", ""),
                "version": rec["version"],
            })
            if len(hits) >= limit:
                break
    return hits


def _active_neighbors(
    state: PeopleState, person_id: str, include_matters: bool
) -> list[tuple[str, str, dict[str, Any]]]:
    """列出单个人物在活动关系边上的直接邻居。"""
    result: list[tuple[str, str, dict[str, Any]]] = []
    for rel in state.relations.values():
        if rel["trashed_at"] is not None:
            continue
        if rel["from_person_id"] == person_id:
            if rel["target_kind"] == "person":
                result.append(("person", rel["target_id"], rel))
            elif include_matters:
                result.append(("matter", rel["target_id"], rel))
        elif rel["target_kind"] == "person" and rel["target_id"] == person_id:
            result.append(("person", rel["from_person_id"], rel))
    return result


def _expand_matter_peers(
    state: PeopleState,
    matter_id: str,
    current_id: str,
    hop: int,
    nodes: dict[str, dict[str, Any]],
    edges: dict[str, dict[str, Any]],
    queue: deque[tuple[str, int]],
) -> None:
    """将挂在同一事项下的其他活动人物并入当前展开结果。"""
    for other_rel in state.relations.values():
        if (
            other_rel["trashed_at"] is not None
            or other_rel["target_kind"] != "matter"
            or other_rel["target_id"] != matter_id
            or other_rel["from_person_id"] == current_id
        ):
            continue
        peer = state.persons.get(other_rel["from_person_id"])
        if peer is None or peer["trashed_at"] is not None or peer["merged_into_id"] is not None:
            continue
        if peer["id"] not in nodes and len(nodes) < 50:
            nodes[peer["id"]] = {"kind": "person", "id": peer["id"], "person": public_person(peer), "hop": hop + 1}
            queue.append((peer["id"], hop + 1))
        if peer["id"] in nodes and len(edges) < 100:
            edges[other_rel["id"]] = public_relation(other_rel)


def expand_graph(
    state: PeopleState, root_id: str, *, depth: int = 2, include_matters: bool = True
) -> dict[str, Any]:
    """以根人物为起点做有界 BFS（1–2 跳，最多 50 节点、100 条边）。"""
    if depth not in (1, 2):
        raise InvalidRequestError("关系图展开深度仅支持 1 或 2")
    root = get_person(state, root_id, active=True)
    nodes: dict[str, dict[str, Any]] = {
        root["id"]: {"kind": "person", "id": root["id"], "person": public_person(root), "hop": 0}
    }
    edges: dict[str, dict[str, Any]] = {}
    queue: deque[tuple[str, int]] = deque([(root["id"], 0)])

    while queue and len(nodes) < 50 and len(edges) < 100:
        current, hop = queue.popleft()
        if hop >= depth:
            continue
        for kind, target_id, rel in _active_neighbors(state, current, include_matters):
            if kind == "person":
                peer = state.persons.get(target_id)
                if peer is None or peer["trashed_at"] is not None or peer["merged_into_id"] is not None:
                    continue
                if target_id not in nodes and len(nodes) < 50:
                    nodes[target_id] = {
                        "kind": "person",
                        "id": target_id,
                        "person": public_person(peer),
                        "hop": hop + 1,
                    }
                    queue.append((target_id, hop + 1))
                if target_id in nodes and len(edges) < 100:
                    edges[rel["id"]] = public_relation(rel)
            else:
                node_key = "matter:" + target_id
                if node_key not in nodes and len(nodes) < 50:
                    nodes[node_key] = {"kind": "matter", "id": target_id, "hop": hop + 1}
                if node_key in nodes and len(edges) < 100:
                    edges[rel["id"]] = public_relation(rel)
                if hop + 1 <= depth:
                    _expand_matter_peers(state, target_id, current, hop, nodes, edges, queue)

    return {"root_id": root["id"], "depth": depth, "nodes": list(nodes.values()), "edges": list(edges.values())}
