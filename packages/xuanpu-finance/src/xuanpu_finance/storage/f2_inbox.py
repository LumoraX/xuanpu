"""验签后回调Inbox、异文保全和人工核对异常的隔离查询。"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, encode_cursor, identifier, page, timestamp_cursor
from xuanpu_finance.storage.f2_worker import SettlementWorkerStore

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class SettlementStore(SettlementWorkerStore):
    """回调只能调度原请求核对，不能按字符串直接宣布付款/入账成功。"""

    async def accept_provider_event(
        self, provider: str, configuration_version: int, event_id: str, raw_hash: str, raw: bytes
    ) -> dict[str, Any]:
        """先保全原始字节再去重，相同事件异文保留第二份原件并建异常。"""
        if len(raw) > 1048576 or hashlib.sha256(raw).hexdigest() != raw_hash:
            raise FinanceError("CALLBACK_RAW_INVALID", 422, "回调字节或摘要无效")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                request_digest([provider, configuration_version, event_id]),
            )
            config = await conn.fetchrow(
                "SELECT * FROM finance_settlement_provider WHERE provider=$1 AND configuration_version=$2",
                provider,
                configuration_version,
            )
            if config is None:
                raise FinanceError("PROVIDER_NOT_FOUND", 404, "固定渠道配置不存在")
            duplicate = await self._existing_provider_event(
                conn, config, provider, configuration_version, event_id, raw_hash, raw
            )
            if duplicate is not None:
                return duplicate
            row, valid = await self._callback_operation(conn, provider, configuration_version, raw)
            status = "accepted" if valid else "rejected"
            await conn.execute(
                "INSERT INTO finance_settlement_inbox(provider,configuration_version,event_id,raw_hash,raw,status,operation_id) VALUES($1,$2,$3,$4,$5,$6,$7)",
                provider,
                configuration_version,
                event_id,
                raw_hash,
                raw,
                status,
                row["id"] if row is not None else None,
            )
            if not valid:
                subject = (
                    row if row is not None else await self._callback_subject(conn, config, None)
                )
                await self._settlement_exception(
                    conn,
                    subject,
                    "CALLBACK_SCOPE_INVALID",
                    {"event_id": event_id, "raw_hash": raw_hash},
                )
                return {"status": "rejected", "event_id": event_id}
            assert row is not None
            await conn.execute(
                "UPDATE finance_settlement_operation SET query_requested=true,next_query_at=now() WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND status IN ('dispatching','awaiting_provider','outcome_unknown')",
                row["tenant_id"],
                row["legal_entity_id"],
                row["id"],
            )
            return {"status": "accepted", "event_id": event_id, "operation_id": row["id"]}

    async def _existing_provider_event(
        self,
        conn: Connection,
        config: Any,
        provider: str,
        configuration_version: int,
        event_id: str,
        raw_hash: str,
        raw: bytes,
    ) -> dict[str, Any] | None:
        """同事件只接受相同原文，异文另存原字节并产生冲突异常。"""
        old = await conn.fetchrow(
            "SELECT * FROM finance_settlement_inbox WHERE provider=$1 AND configuration_version=$2 AND event_id=$3",
            provider,
            configuration_version,
            event_id,
        )
        if old is not None:
            if old["raw_hash"] == raw_hash:
                return {"status": "duplicate", "event_id": event_id}
            await conn.execute(
                "INSERT INTO finance_settlement_inbox_conflict(id,provider,configuration_version,event_id,raw_hash,raw) VALUES($1,$2,$3,$4,$5,$6)",
                identifier(),
                provider,
                configuration_version,
                event_id,
                raw_hash,
                raw,
            )
            subject = await self._callback_subject(conn, config, old["operation_id"])
            await self._settlement_exception(
                conn,
                subject,
                "CALLBACK_EVENT_CONFLICT",
                {
                    "event_id": event_id,
                    "raw_hash": raw_hash,
                    "original_raw_hash": old["raw_hash"],
                },
            )
            return {"status": "conflict", "event_id": event_id}
        return None

    async def _callback_operation(
        self, conn: Connection, provider: str, configuration_version: int, raw: bytes
    ) -> tuple[Any, bool]:
        """入站范围精确核验，回调仍只可调度查单。"""
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            body = None
        row = None
        if isinstance(body, dict) and isinstance(body.get("external_request_id"), str):
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_operation WHERE provider=$1 AND configuration_version=$2 AND external_request_id=$3",
                provider,
                configuration_version,
                body["external_request_id"],
            )
        valid = row is not None and row["request"] is not None and isinstance(body, dict)
        if valid:
            assert row is not None
            request = decode(row["request"])
            valid = all(
                body.get(k) == request[k]
                for k in (
                    "tenant_id",
                    "legal_entity_id",
                    "provider",
                    "configuration_version",
                    "simulation",
                )
            )
            valid = valid and all(
                k not in body or body[k] == request[k]
                for k in (
                    "amount",
                    "currency",
                    "payload_hash",
                    "operation_id",
                    "action",
                    "target",
                    "plan_digest",
                )
            )
        return row, valid

    async def _callback_subject(
        self, conn: Connection, config: Any, operation_id: str | None
    ) -> Any:
        """未知请求异常仍归固定配置公司，保全时不伪造真实业务单据。"""
        if operation_id:
            row = await conn.fetchrow(
                "SELECT * FROM finance_settlement_operation WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3",
                config["tenant_id"],
                config["legal_entity_id"],
                operation_id,
            )
            if row is not None:
                return row
        return {
            "tenant_id": config["tenant_id"],
            "legal_entity_id": config["legal_entity_id"],
            "owner_id": "finance-system",
            "id": operation_id or "unknown-provider-request",
        }

    async def list_exceptions(
        self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """F0原件异常和F2外部核对异常过滤后统一计数/分页。"""
        scope = request_digest([who.tenant_id, who.user_id, "exceptions", entity])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            role = await self._role(conn, who, entity)
            union = "SELECT id,tenant_id,legal_entity_id,owner_id,version,rule_id,status,severity,received_at,receipt_id,NULL::jsonb AS settlement_evidence FROM finance_exception UNION ALL SELECT id,tenant_id,legal_entity_id,owner_id,version,rule_id,status,severity,created_at,NULL::text,evidence FROM finance_settlement_exception"
            where = "tenant_id=$1 AND legal_entity_id=$2 AND (owner_id=$3 OR $4)"
            args = (who.tenant_id, entity, who.user_id, role == "reviewer")
            total = await conn.fetchval(
                "SELECT count(*) FROM (" + union + ") e WHERE " + where, *args
            )
            rows = await conn.fetch(
                "SELECT * FROM ("
                + union
                + ") e WHERE "
                + where
                + " AND ($5::timestamptz IS NULL OR (received_at,id)>($5,$6::text)) ORDER BY received_at,id LIMIT $7",
                *args,
                stamp,
                last,
                limit + 1,
            )
            items = []
            for row in rows[:limit]:
                if row["settlement_evidence"] is None:
                    items.append(await self._exception_case(conn, row))
                    continue
                evidence = decode(row["settlement_evidence"])
                digest = evidence.get("receipt_sha256", evidence.get("raw_hash"))
                item = {
                    k: row[k]
                    for k in ("id", "version", "legal_entity_id", "rule_id", "status", "severity")
                }
                item["evidence"] = (
                    [
                        {
                            "id": row["id"],
                            "kind": "provider_receipt",
                            "sha256": digest,
                            "verification_state": "not_checked",
                        }
                    ]
                    if digest
                    else []
                )
                items.append(item)
            return page(
                items,
                total,
                encode_cursor(
                    scope, [rows[limit - 1]["received_at"].isoformat(), rows[limit - 1]["id"]]
                )
                if len(rows) > limit
                else None,
            )

    async def list_integrations(self, who: FinancePrincipal, entity: str) -> dict[str, Any]:
        """只按实际配置声明渠道能力，不把未验收正式通道标成已接入。"""
        result = await super().list_integrations(who, entity)
        async with self.pool.acquire() as conn:
            policy = await self._settlement_policy(conn, who.tenant_id, entity, require=False)
            if policy is None:
                return result
            for item in result["items"]:
                prefix = (
                    "payment"
                    if item["kind"] == "payment"
                    else "posting"
                    if item["kind"] == "erp"
                    else None
                )
                if prefix is None:
                    continue
                provider = await conn.fetchrow(
                    "SELECT * FROM finance_settlement_provider WHERE tenant_id=$1 AND legal_entity_id=$2 AND provider=$3 AND configuration_version=$4",
                    who.tenant_id,
                    entity,
                    policy[prefix + "_provider"],
                    policy[prefix + "_configuration_version"],
                )
                if provider is None:
                    continue
                data = decode(provider["data"])
                item.update(
                    {
                        "status": "ready"
                        if provider["enabled"] and data["simulation"]
                        else "disabled"
                        if not provider["enabled"]
                        else "degraded",
                        "capabilities": ["simulation", "idempotency", "query"]
                        if data["simulation"]
                        else ["http_protocol_configured"],
                        "provider": provider["provider"],
                        "simulation": data["simulation"],
                    }
                )
            return result
