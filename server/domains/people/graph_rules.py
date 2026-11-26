"""个人知识图谱规则：公司/组织档案、跨实体关系规范化与 1–3 跳图谱遍历。"""

from __future__ import annotations

import secrets
from collections import deque
from copy import deepcopy
from typing import Any

from server.core.errors import ConflictError, NotFoundError
from server.domains.people.models import (
    MAX_ORGS,
    SYMMETRIC_TYPES,
    PeopleState,
    public_note,
    public_relation,
)
from server.domains.people.rules import _merge_unique_strings

ORG_SYMMETRIC_TYPES = frozenset({"partner_org", "partner", "affiliate", "合作机构", "合作伙伴"})


def new_org_record(profile: dict[str, Any], now: float) -> dict[str, Any]:
    """构造一条新的公司/组织领域记录。"""
    return {
        "id": "org_" + secrets.token_hex(8),
        "name": profile["name"],
        "profile": deepcopy(profile),
        "version": 1,
        "merged_into_id": None,
        "merge_snapshot": None,
        "trashed_at": None,
        "created_at": now,
        "updated_at": now,
    }


def sync_org_profile(record: dict[str, Any], profile: dict[str, Any], now: float) -> None:
    """更新公司/组织 profile 时同步名称与版本号。"""
    record["name"] = profile["name"]
    record["profile"] = deepcopy(profile)
    record["version"] += 1
    record["updated_at"] = now


def get_org(state: PeopleState, org_id: str, *, active: bool = False) -> dict[str, Any]:
    """按标识取公司/组织记录；可选要求未进回收站且未被合并。"""
    rec = state.orgs.get(org_id)
    if rec is None:
        raise NotFoundError("公司/组织档案不存在")
    if active and (rec["trashed_at"] is not None or rec["merged_into_id"] is not None):
        raise NotFoundError("公司/组织档案不可用或已进回收站")
    return rec


def matches_org(
    record: dict[str, Any],
    *,
    q: str | None = None,
    city: str | None = None,
    tag: str | None = None,
) -> bool:
    """按名称、别名、地址、行业、城市、标签或备注过滤公司档案。"""
    profile = record["profile"]
    if city and city.casefold() not in str(profile.get("city", "")).casefold():
        return False
    if tag and all(t.casefold() != tag.casefold() for t in profile.get("tags", [])):
        return False
    if q:
        needle = q.casefold()
        hay = " ".join(
            [
                record["name"],
                *profile.get("aliases", []),
                profile.get("org_type", ""),
                profile.get("industry", ""),
                profile.get("city", ""),
                profile.get("address", ""),
                profile.get("summary", ""),
                *profile.get("tags", []),
            ]
        ).casefold()
        if needle not in hay:
            return False
    return True


def find_similar_orgs(state: PeopleState, profile: dict[str, Any]) -> list[dict[str, Any]]:
    """按全称、简称或包含关系查找已有疑似重复公司/组织。"""
    target_names = {profile["name"].strip().casefold()} | {
        a.strip().casefold() for a in profile.get("aliases", []) if a.strip()
    }
    hits: list[dict[str, Any]] = []
    for rec in state.orgs.values():
        if rec["trashed_at"] is not None or rec["merged_into_id"] is not None:
            continue
        p = rec["profile"]
        existing_names = {rec["name"].strip().casefold()} | {
            a.strip().casefold() for a in p.get("aliases", []) if a.strip()
        }
        reason: str | None = None
        if target_names & existing_names:
            reason = "名称或简称一致"
        elif any(
            len(a) >= 2 and len(b) >= 2 and (a in b or b in a)
            for a in target_names
            for b in existing_names
        ):
            reason = "名称相近"
        if reason is not None:
            hits.append(
                {
                    "org_id": rec["id"],
                    "name": rec["name"],
                    "city": p.get("city", ""),
                    "version": rec["version"],
                    "reason": reason,
                }
            )
    return hits[:5]


def merge_org_profile_patch(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """将提案中的增量公司更新安全合并到已有公司档案，保留既有简称、地址与备注。"""
    merged: dict[str, Any] = deepcopy(existing)
    for field_name in ("name", "org_type", "industry", "city", "address", "website"):
        val = incoming.get(field_name)
        if isinstance(val, str) and val.strip():
            merged[field_name] = val.strip()
    in_summary = str(incoming.get("summary") or "").strip()
    ex_summary = str(existing.get("summary") or "").strip()
    if in_summary:
        if not ex_summary or ex_summary in in_summary:
            merged["summary"] = in_summary[:2000]
        elif in_summary not in ex_summary:
            merged["summary"] = f"{ex_summary}；{in_summary}"[:2000]
    merged["aliases"] = _merge_unique_strings(existing.get("aliases", []), incoming.get("aliases", []), 20)
    merged["tags"] = _merge_unique_strings(existing.get("tags", []), incoming.get("tags", []), 30)
    extra = dict(existing.get("extra", {}))
    for k, v in (incoming.get("extra") or {}).items():
        if isinstance(v, str) and v.strip():
            extra[k] = v.strip()
    merged["extra"] = dict(list(extra.items())[:30])
    return merged


def canonical_entity_relation(
    from_kind: str,
    from_id: str,
    target_kind: str,
    target_id: str,
    relation_type: str,
    symmetric: bool,
) -> tuple[str, str, str, str, bool]:
    """规范化跨实体关系边端点，避免对称关系存两条重复边。"""
    if from_kind == "matter" or target_kind == "matter":
        return from_kind, from_id, target_kind, target_id, False
    is_sym = bool(
        symmetric
        or (from_kind == "person" and target_kind == "person" and relation_type.casefold() in SYMMETRIC_TYPES)
        or (from_kind == "org" and target_kind == "org" and relation_type.casefold() in ORG_SYMMETRIC_TYPES)
    )
    if is_sym and from_kind == target_kind and from_id > target_id:
        return target_kind, target_id, from_kind, from_id, True
    return from_kind, from_id, target_kind, target_id, is_sym


def check_duplicate_entity_relation(
    state: PeopleState,
    *,
    from_kind: str,
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
    key = (from_kind, from_id, target_kind, target_id, relation_type.casefold())
    for rel in state.relations.values():
        if rel["id"] == exclude_id or rel["trashed_at"] is not None or rel["valid_to"] is not None:
            continue
        existing = (
            rel.get("from_kind", "person"),
            rel["from_person_id"],
            rel["target_kind"],
            rel["target_id"],
            rel["relation_type"].casefold(),
        )
        if existing == key:
            raise ConflictError("已存在相同的有效关系记录，请直接更新既有关系")


def _node_view(
    state: PeopleState,
    kind: str,
    entity_id: str,
    matters_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """把图谱中的 person / org / matter 节点统一投影为轻量节点视图。"""
    if kind == "person":
        rec = state.persons.get(entity_id)
        if rec is None or rec["trashed_at"] is not None or rec["merged_into_id"] is not None:
            return None
        p = rec["profile"]
        return {
            "kind": "person",
            "id": rec["id"],
            "name": rec["name"],
            "organization": rec["organization"],
            "subtitle": " · ".join(x for x in (rec["organization"], p.get("title", ""), p.get("city", "")) if x),
            "summary": p.get("summary", ""),
            "version": rec["version"],
        }
    if kind == "org":
        rec = state.orgs.get(entity_id)
        if rec is None or rec["trashed_at"] is not None or rec["merged_into_id"] is not None:
            return None
        p = rec["profile"]
        return {
            "kind": "org",
            "id": rec["id"],
            "name": rec["name"],
            "subtitle": " · ".join(x for x in (p.get("org_type", ""), p.get("industry", ""), p.get("city", "")) if x),
            "summary": p.get("summary", ""),
            "version": rec["version"],
        }
    if kind == "matter":
        mat = matters_by_id.get(entity_id)
        if mat is None:
            return {"kind": "matter", "id": entity_id, "name": entity_id, "subtitle": "", "summary": "", "version": 1}
        data = mat.get("data") or {}
        return {
            "kind": "matter",
            "id": entity_id,
            "name": str(data.get("title") or entity_id),
            "subtitle": str(data.get("status") or "active"),
            "summary": str(data.get("description") or "")[:300],
            "version": mat.get("version", 1),
        }
    return None


def expand_entity_graph(
    state: PeopleState,
    start_kind: str,
    start_id: str,
    *,
    depth: int = 2,
    matters_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """以任意 person / org / matter 为中心，做 1–3 跳跨实体关系图遍历并收集溯源记录。"""
    matters = matters_by_id or {}
    bounded_depth = max(1, min(3, depth))
    start_key = (start_kind, start_id)
    visited: set[tuple[str, str]] = {start_key}
    queue: deque[tuple[str, str, int]] = deque([(start_kind, start_id, 0)])
    edges_map: dict[str, dict[str, Any]] = {}
    source_note_ids: set[str] = set()

    while queue:
        cur_kind, cur_id, step = queue.popleft()
        if step >= bounded_depth:
            continue
        for rel in state.relations.values():
            if rel["trashed_at"] is not None:
                continue
            fk = rel.get("from_kind", "person")
            fid = rel["from_person_id"]
            tk = rel["target_kind"]
            tid = rel["target_id"]
            if (fk, fid) == (cur_kind, cur_id):
                nxt = (tk, tid)
            elif (tk, tid) == (cur_kind, cur_id):
                nxt = (fk, fid)
            else:
                continue
            edges_map[rel["id"]] = rel
            if rel.get("source_note_id"):
                source_note_ids.add(rel["source_note_id"])
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt[0], nxt[1], step + 1))

    nodes: list[dict[str, Any]] = []
    for n_kind, n_id in visited:
        view = _node_view(state, n_kind, n_id, matters)
        if view is not None:
            nodes.append(view)

    # 收集所有直接关联到中心实体或出现在关系溯源里的活动原始记录
    record_ids: set[str] = set(source_note_ids)
    for note in state.notes.values():
        if note["trashed_at"] is not None:
            continue
        if start_kind == "person" and start_id in note.get("person_ids", []):
            record_ids.add(note["id"])
        elif start_kind == "org" and start_id in note.get("org_ids", []):
            record_ids.add(note["id"])
        elif start_kind == "matter" and (
            note.get("matter_id") == start_id or start_id in note.get("matter_ids", [])
        ):
            record_ids.add(note["id"])

    records = [
        public_note(state.notes[nid])
        for nid in record_ids
        if nid in state.notes and state.notes[nid]["trashed_at"] is None
    ]
    records.sort(key=lambda n: (-n["occurred_at"], n["id"]))
    edges = [public_relation(r) for r in sorted(edges_map.values(), key=lambda r: (r["created_at"], r["id"]))]
    return {
        "center": {"kind": start_kind, "id": start_id},
        "depth": bounded_depth,
        "nodes": nodes,
        "edges": edges,
        "records": records[:30],
    }


def _active_entity_exists(
    state: PeopleState,
    kind: str,
    entity_id: str,
    matters_by_id: dict[str, dict[str, Any]],
) -> bool:
    """判断节点标识是否对应一条活动（未回收、未合并）的实体记录。"""
    if kind == "person":
        rec = state.persons.get(entity_id)
        return rec is not None and rec["trashed_at"] is None and rec["merged_into_id"] is None
    if kind == "org":
        rec = state.orgs.get(entity_id)
        return rec is not None and rec["trashed_at"] is None and rec["merged_into_id"] is None
    if kind == "matter":
        mat = matters_by_id.get(entity_id)
        return mat is not None and mat.get("trashed_at") is None
    return False


def _entity_updated_at(
    state: PeopleState,
    kind: str,
    entity_id: str,
    matters_by_id: dict[str, dict[str, Any]],
) -> float:
    """读取实体最近更新时间，用于全图截断时优先保留活跃节点。"""
    if kind == "person":
        return float(state.persons[entity_id].get("updated_at", 0.0))
    if kind == "org":
        return float(state.orgs[entity_id].get("updated_at", 0.0))
    if kind == "matter":
        return float(matters_by_id[entity_id].get("updated_at", 0.0))
    return 0.0


def _active_entity_keys(
    state: PeopleState,
    matters_by_id: dict[str, dict[str, Any]],
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    """收集全部活动实体键及各类数量统计。"""
    persons = [
        ("person", rec["id"])
        for rec in state.persons.values()
        if rec["trashed_at"] is None and rec["merged_into_id"] is None
    ]
    orgs = [
        ("org", rec["id"])
        for rec in state.orgs.values()
        if rec["trashed_at"] is None and rec["merged_into_id"] is None
    ]
    matters = [
        ("matter", mid)
        for mid, mat in matters_by_id.items()
        if mat.get("trashed_at") is None
    ]
    counts = {
        "total_persons": len(persons),
        "total_orgs": len(orgs),
        "total_matters": len(matters),
    }
    return [*persons, *orgs, *matters], counts


def build_full_graph(
    state: PeopleState,
    *,
    matters_by_id: dict[str, dict[str, Any]] | None = None,
    limit: int = 200,
    include_isolated: bool = False,
) -> dict[str, Any]:
    """构建本人个人空间的全景关系图谱，按连接度与活跃度有界截断返回。"""
    matters = matters_by_id or {}
    bounded_limit = max(1, min(500, limit))
    all_keys, counts = _active_entity_keys(state, matters)

    active_rels: list[dict[str, Any]] = []
    degrees: dict[tuple[str, str], int] = {}
    for rel in state.relations.values():
        if rel["trashed_at"] is not None:
            continue
        src = (rel.get("from_kind", "person"), rel["from_person_id"])
        dst = (rel["target_kind"], rel["target_id"])
        if not (
            _active_entity_exists(state, src[0], src[1], matters)
            and _active_entity_exists(state, dst[0], dst[1], matters)
        ):
            continue
        active_rels.append(rel)
        degrees[src] = degrees.get(src, 0) + 1
        degrees[dst] = degrees.get(dst, 0) + 1

    candidates = all_keys if include_isolated else list(degrees.keys())
    candidates.sort(
        key=lambda k: (-degrees.get(k, 0), -_entity_updated_at(state, k[0], k[1], matters), k[0], k[1])
    )
    truncated = len(candidates) > bounded_limit
    picked = candidates[:bounded_limit]
    picked_set = set(picked)

    nodes: list[dict[str, Any]] = []
    for kind, eid in picked:
        view = _node_view(state, kind, eid, matters)
        if view is not None:
            view["degree"] = degrees.get((kind, eid), 0)
            nodes.append(view)

    shown_rels = [
        rel
        for rel in active_rels
        if (rel.get("from_kind", "person"), rel["from_person_id"]) in picked_set
        and (rel["target_kind"], rel["target_id"]) in picked_set
    ]
    shown_rels.sort(key=lambda r: (r["created_at"], r["id"]))
    edges = [public_relation(r) for r in shown_rels]
    return {
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        "stats": {
            **counts,
            "total_edges": len(active_rels),
            "shown_nodes": len(nodes),
            "shown_edges": len(edges),
        },
    }


__all__ = [
    "MAX_ORGS",
    "ORG_SYMMETRIC_TYPES",
    "build_full_graph",
    "canonical_entity_relation",
    "check_duplicate_entity_relation",
    "expand_entity_graph",
    "find_similar_orgs",
    "get_org",
    "matches_org",
    "merge_org_profile_patch",
    "new_org_record",
    "sync_org_profile",
]
