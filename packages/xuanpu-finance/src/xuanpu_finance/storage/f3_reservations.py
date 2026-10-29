"""税务确认维度和父清册事件的在途占用，不释放未知外部结果。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.storage.business_base import Connection

INFLIGHT = ["accepted", "running", "dispatching", "awaiting_provider", "outcome_unknown"]


async def assert_compliance_dimension(conn: Connection, plan: Any, *, exclude_operation_id: str | None = None) -> None:
    """调用者已持有排序目标票据锁，同票同维度或补包事件只能有一个在途请求。"""
    payload = decode(plan["data"])
    action = payload["action"]
    scope = (plan["tenant_id"], plan["legal_entity_id"])
    if action in {"confirm_usage", "confirm_tax_entry"}:
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND action=$3 AND target_type='invoice' AND target_id=$4 AND status=ANY($5::text[]) AND ($6::text IS NULL OR id<>$6))", *scope, action, payload["target_id"], INFLIGHT, exclude_operation_id)
    elif action == "confirm_red_letter":
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND action='confirm_red_letter' AND request_template#>>'{payload,parameters,confirmation_id}'=$3 AND status=ANY($4::text[]) AND ($5::text IS NULL OR id<>$5))", *scope, payload["parameters"]["confirmation_id"], INFLIGHT, exclude_operation_id)
    elif action == "archive" and payload["parameters"].get("parent_manifest_hash") is not None:
        exists = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND action='archive' AND request_template#>>'{payload,parameters,parent_manifest_hash}'=$3 AND (request_template#>'{payload,parameters,source_event_ids}') ?| $4::text[] AND status=ANY($5::text[]) AND ($6::text IS NULL OR id<>$6))", *scope, payload["parameters"]["parent_manifest_hash"], payload["parameters"]["source_event_ids"], INFLIGHT, exclude_operation_id)
    else:
        return
    if exists:
        raise FinanceError("COMPLIANCE_DIMENSION_INFLIGHT", 409, "同一税务维度或父清册事件已有在途请求，未知结果不得重新发起")
