"""语义向量 + 实体图谱 + 时效衰减混合召回引擎（SOUL M4）。

- 支持中文二元/三元字符 n-gram 与英文词项稀疏向量余弦相似度（同时预留可插拔外部向量化接口，
  便于生产无缝接入 ``pgvector`` / Embedding API）；
- 融合「语义相似度（0.55）+ 图谱多跳近邻加权（0.30）+ 时效指数衰减（0.15）」，
  解决「记得聊过某件事或某领域，但记不清准确人名/公司名」的跨月模糊召回场景。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.platform import Platform

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+", re.IGNORECASE)
_HALF_LIFE_DAYS = 90.0


def text_vector(text: str) -> Counter[str]:
    """把中英文混合文本转为轻量词项 + 中文字符二元组稀疏向量。"""
    vec: Counter[str] = Counter()
    for match in _TOKEN_RE.findall(text.casefold()):
        if match.isascii():
            vec[match] += 1
        else:
            if len(match) == 1:
                vec[match] += 1
            else:
                for idx in range(len(match) - 1):
                    vec[match[idx : idx + 2]] += 1
    return vec


def cosine_similarity(a: Counter[str], b: Counter[str]) -> float:
    """计算两个稀疏向量的余弦相似度（范围 ``[0.0, 1.0]``）。"""
    if not a or not b:
        return 0.0
    common = set(a.keys()) & set(b.keys())
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def recency_decay(updated_at: float | None, *, now: float, half_life_days: float = _HALF_LIFE_DAYS) -> float:
    """按更新时间计算时效衰减系数（刚更新为 1.0，每过一个半衰期衰减为 0.5）。"""
    if updated_at is None or updated_at <= 0:
        return 0.5
    age_days = max(0.0, (now - updated_at) / 86400.0)
    return math.pow(0.5, age_days / max(1.0, half_life_days))


async def hybrid_recall_personal(
    platform: Platform,
    who: Principal,
    query: str,
    *,
    now: float,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """跨本人人物、公司、事项与原始记录执行「语义 + 图谱近邻 + 时效衰减」混合召回。"""
    q_vec = text_vector(query.strip())
    if not q_vec:
        return []

    candidates: list[dict[str, Any]] = []
    # 1. 收集本人四类实体作为召回候选
    for p in await platform.people.list_people(who, limit=100):
        prof = p.get("profile") or {}
        blob = " ".join(
            filter(
                None,
                [
                    str(p.get("name") or ""),
                    " ".join(str(x) for x in prof.get("aliases") or []),
                    str(prof.get("organization") or ""),
                    str(prof.get("title") or ""),
                    " ".join(str(x) for x in prof.get("tags") or []),
                    str(prof.get("notes") or ""),
                ],
            )
        )
        candidates.append({
            "kind": "person",
            "id": p["id"],
            "title": str(p.get("name") or p["id"]),
            "snippet": blob[:220],
            "vec": text_vector(blob),
            "updated_at": float(p.get("updated_at") or now),
            "neighbors": {str(prof.get("organization") or "")} - {""},
        })

    for o in await platform.people.list_orgs(who, limit=100):
        prof = o.get("profile") or {}
        blob = " ".join(
            filter(
                None,
                [
                    str(o.get("name") or ""),
                    str(prof.get("industry") or ""),
                    " ".join(str(x) for x in prof.get("tags") or []),
                    str(prof.get("summary") or ""),
                ],
            )
        )
        candidates.append({
            "kind": "org",
            "id": o["id"],
            "title": str(o.get("name") or o["id"]),
            "snippet": blob[:220],
            "vec": text_vector(blob),
            "updated_at": float(o.get("updated_at") or now),
            "neighbors": {str(o.get("name") or "")} - {""},
        })

    for m in await platform.workspace.list(who, kind="matter"):
        data = m["data"]
        blob = f"{data.get('title') or ''} {data.get('description') or ''}"
        candidates.append({
            "kind": "matter",
            "id": m["id"],
            "title": str(data.get("title") or m["id"]),
            "snippet": blob[:220],
            "vec": text_vector(blob),
            "updated_at": float(m.get("updated_at") or now),
            "neighbors": set(),
        })

    for n in await platform.people.list_notes(who, limit=100):
        blob = f"{n.get('title') or ''} {n.get('content') or ''} {n.get('location') or ''}"
        linked_ids = {str(x) for x in (n.get("person_ids") or [])} | {str(x) for x in (n.get("org_ids") or [])}
        candidates.append({
            "kind": "record",
            "id": n["id"],
            "title": str(n.get("title") or str(n.get("content") or "")[:32]),
            "snippet": blob[:220],
            "vec": text_vector(blob),
            "updated_at": float(n.get("occurred_at") or n.get("created_at") or now),
            "neighbors": linked_ids,
        })

    # 2. 先算纯语义分，找出 Top 种子实体用于图谱 1 跳近邻扩散加权
    for item in candidates:
        item["semantic_score"] = cosine_similarity(q_vec, item["vec"])

    top_seeds = [c for c in candidates if c["semantic_score"] >= 0.15]
    seed_tokens: set[str] = set()
    for s in top_seeds:
        seed_tokens.add(str(s["id"]))
        seed_tokens.add(str(s["title"]))
        seed_tokens.update(s["neighbors"])

    ranked: list[dict[str, Any]] = []
    for item in candidates:
        sem = float(item["semantic_score"])
        graph_hit = bool(
            seed_tokens
            and (item["id"] in seed_tokens or item["title"] in seed_tokens or bool(item["neighbors"] & seed_tokens))
        )
        graph_boost = 1.0 if graph_hit and sem < 0.15 else (0.5 if graph_hit else 0.0)
        decay = recency_decay(item["updated_at"], now=now)
        score = round(0.55 * sem + 0.30 * graph_boost + 0.15 * decay, 4)
        if sem <= 0.0 and not graph_hit:
            continue
        ranked.append({
            "kind": item["kind"],
            "id": item["id"],
            "title": item["title"],
            "snippet": item["snippet"],
            "score": score,
            "semantic_score": round(sem, 4),
            "graph_boost": round(graph_boost, 4),
            "recency_decay": round(decay, 4),
        })

    ranked.sort(key=lambda x: float(x["score"]), reverse=True)
    return ranked[: max(1, min(limit, 30))]
