"""导入操作对外快照与内容摘要。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def request_digest(value: object) -> str:
    """稳定序列化计算幂等请求摘要。"""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def operation_snapshot(row: Any, items: list[Any]) -> dict[str, Any]:
    """将持久逐项进度投影为契约快照，received 计入 pending。"""
    counts = dict.fromkeys(
        (
            "pending",
            "parsed",
            "duplicate_linked",
            "awaiting_original",
            "quarantined",
            "failed",
            "cancelled",
        ),
        0,
    )
    result_items: list[dict[str, Any]] = []
    resources = []
    refs = []
    for item in items:
        ref = (
            json.loads(item["file_ref"]) if isinstance(item["file_ref"], str) else item["file_ref"]
        )
        entry = {"file_id": ref["file_id"], "state": item["state"]}
        for name in ("invoice_id", "exception_id", "error_code"):
            if item[name] is not None:
                entry[name] = item[name]
        result_items.append(entry)
        counts["pending" if item["state"] == "received" else item["state"]] += 1
        if item["invoice_id"]:
            resources.append({"kind": "invoice", "id": item["invoice_id"]})
        if item["exception_id"]:
            resources.append({"kind": "exception", "id": item["exception_id"]})
        if item["receipt_id"]:
            refs.append(
                {
                    "id": item["receipt_id"],
                    "kind": "source",
                    "sha256": item["actual_sha256"],
                    "verification_state": "not_checked",
                    "source_version": str(ref["revision"]),
                }
            )
    return {
        "id": row["id"],
        "version": row["version"],
        "status": row["status"],
        "business_result": row["business_result"],
        "trace_id": row["trace_id"],
        "items": result_items,
        "result_refs": refs,
        "resources": resources[:100],
        "summary": {"total": len(items), **counts},
    }
