"""F3合规记录、实际待办和档案事实的公司内权限查询。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.common import decode, encode_cursor, page, timestamp_cursor
from xuanpu_finance.storage.f3_archive_health import archive_source_changes
from xuanpu_finance.storage.f3_worker import ComplianceWorkerStore
from xuanpu_finance.storage.f4_visibility import visibility

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection

TABLES = {"plans": "finance_compliance_plan", "verifications": "finance_invoice_verification", "confirmations": "finance_tax_confirmation", "red_cases": "finance_red_letter_case", "adjustments": "finance_tax_adjustment", "archives": "finance_archive", "supplements": "finance_archive_supplement", "audits": "finance_archive_audit"}


def public_archive_snapshot(value: Any) -> Any:
    """递归剔除内部对象来源和持久请求，公开业务清册不泄露对象路径。"""
    if isinstance(value, dict):
        return {k: public_archive_snapshot(v) for k, v in value.items() if k not in {"source", "provider_request", "request", "object_key", "content_base64", "bindings", "request_template"}}
    if isinstance(value, list):
        return [public_archive_snapshot(v) for v in value]
    return value


class ComplianceQueryStore(ComplianceWorkerStore):
    """权限过滤后独立计数，历史事实与当前健康状态分开呈现。"""

    async def get_invoice_compliance(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """返回独立合规版本和真实权威快照，不改写票据content。"""
        async with self.pool.acquire() as conn, conn.transaction():
            invoice = await self._f3_invoice(conn, who, id)
            state = await self._f3_state(conn, invoice)
            data = await self._invoice_snapshot(conn, who.tenant_id, invoice["legal_entity_id"], invoice["data"])
            return {"id": id, "invoice_id": id, "legal_entity_id": invoice["legal_entity_id"], "content_version": decode(invoice["data"])["version"], "version": state["version"], "authority_revision": state["authority_revision"], "provider": state.get("provider"), "configuration_version": state.get("configuration_version"), "provider_generation": state.get("provider_generation"), "verification_status": data["verification_status"], "invoice_status": data["compliance_status"], "valid_until": data["verification_evidence"].get("effective_valid_until") if isinstance(data.get("verification_evidence"), dict) else None, "snapshot": state["snapshot"], "effective_gross": data["effective_gross"], "effective_tax": data["effective_tax"], "available_amount": data["available_amount"], "available_tax": data["available_tax"], "allocation_shortfall": data["allocation_shortfall"], "allocation_frozen": data["allocation_frozen"], "simulation": invoice["source_simulation"]}

    async def list_invoice_verifications(self, who: FinancePrincipal, id: str, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """查验历史保留旧权威版本及是否应用，原始字节不作覆盖。"""
        async with self.pool.acquire() as conn:
            invoice = await self._f3_invoice(conn, who, id)
        return await self._f3_list(who, "verifications", invoice["legal_entity_id"], cursor, limit, parent_id=id)

    async def get_compliance_options(self, who: FinancePrincipal, company: str) -> dict[str, Any]:
        """只按当前策略和真实岗位给出能力，正式标准未验收始终关闭。"""
        async with self.pool.acquire() as conn, conn.transaction():
            role = await self._role(conn, who, company)
            policy = await self._compliance_policy(conn, who.tenant_id, company, require=False)
            simulation = await conn.fetchval("SELECT simulation FROM finance_compliance_mode WHERE singleton")
            return {"legal_entity_id": company, "enabled": policy is not None, "simulation": simulation is True, "tax_enabled": policy is not None and policy["tax_enabled"], "archive_enabled": policy is not None and policy["archive_enabled"] and simulation is True, "policy_version": policy["version"] if policy is not None else None, "tax_executor": policy is not None and role == "reviewer" and who.user_id in policy["tax_executor_user_ids"], "archive_executor": policy is not None and role == "reviewer" and who.user_id in policy["archive_executor_user_ids"], "open_periods": policy["open_periods"] if policy is not None else [], "supported_invoice_profiles": policy["supported_invoice_profiles"] if policy is not None else [], "archive_profile_id": policy["archive_profile_id"] if policy is not None else None, "archive_profile_version": policy["archive_profile_version"] if policy is not None else None, "formal_profile_state": "unverified"}

    async def list_compliance_plans(self, who: FinancePrincipal, company: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """专用行动计划支持当前实际指派的最小读取权。"""
        return await self._f3_list(who, "plans", company, cursor, limit)

    async def list_tax_confirmations(self, who: FinancePrincipal, company: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """税务确认记录不能被解释为红票开具或申报成功。"""
        return await self._f3_list(who, "confirmations", company, cursor, limit)

    async def list_red_letter_cases(self, who: FinancePrincipal, company: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """列出外部已经存在的红字确认单。"""
        return await self._f3_list(who, "red_cases", company, cursor, limit)

    async def get_red_letter_case(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """查看已有权威确认单和独立红票人工关联事实。"""
        return await self._f3_get(who, "red_cases", id)

    async def list_tax_adjustments(self, who: FinancePrincipal, company: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """税务调整义务来源于权威快照，材料收到不改权威状态。"""
        return await self._f3_list(who, "adjustments", company, cursor, limit)

    async def list_archives(self, who: FinancePrincipal, company: str | None = None, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """档案保留原封存事实，健康状态另行追加。"""
        return await self._f3_list(who, "archives", company, cursor, limit)

    async def get_archive(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """读取档案事实及当前是否需要补充封存。"""
        return await self._f3_get(who, "archives", id)

    async def get_archive_manifest(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """返回固定清册的实际对象版本引用与冻结来源快照。"""
        archive = await self._f3_get(who, "archives", id)
        return {"archive_id": id, "manifest_hash": archive["manifest_hash"], "manifest_member_id": archive["data"]["manifest_member_id"], "members": archive["data"]["members"], "snapshot": archive["snapshot"]}

    async def get_archive_tests(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """封存四性检测不可变，后续审计不重写原检测。"""
        archive = await self._f3_get(who, "archives", id)
        return {"archive_id": id, "source_snapshot_hash": archive["source_snapshot_hash"], "items": archive["data"]["four_qualities"]}

    async def get_archive_evidence(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """只返回当前授权档案的实际版本锁与保留证据。"""
        archive = await self._f3_get(who, "archives", id)
        return {"archive_id": id, "seal_member_id": archive["data"]["seal_member_id"], "members": archive["data"]["members"], "readback_verified": archive["data"]["readback_verified"]}

    async def list_archive_supplements(self, who: FinancePrincipal, id: str, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """补充封存关系以父清册与来源事件稳定去重。"""
        archive = await self._f3_get(who, "archives", id)
        return await self._f3_list(who, "supplements", archive["legal_entity_id"], cursor, limit, parent_id=id)

    async def get_archive_audits(self, who: FinancePrincipal, id: str, cursor: str | None = None, limit: int = 50) -> dict[str, Any]:
        """只追加原档案的后续真实读回健康事实。"""
        archive = await self._f3_get(who, "archives", id)
        return await self._f3_list(who, "audits", archive["legal_entity_id"], cursor, limit, parent_id=id)

    async def _f3_visible(self, conn: Connection, who: FinancePrincipal, row: Any, name: str) -> bool:
        """无公司reviewer权限时检查原owner或实际有效审批指派。"""
        if row["tenant_id"] != who.tenant_id:
            return False
        role = await conn.fetchval("SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3", who.tenant_id, row["legal_entity_id"], who.user_id)
        if role is None:
            return False
        if role == "reviewer" or row["owner_id"] == who.user_id or (name == "plans" and row["actor_id"] == who.user_id):
            return True
        if name in {"verifications", "confirmations", "red_cases", "adjustments"}:
            return await self._f3_assigned(conn, who, row["legal_entity_id"], "invoice", row["invoice_id"])
        if name == "archives":
            if await self._f3_assigned(conn, who, row["legal_entity_id"], row["target_type"], row["target_id"]):
                return True
            table = "finance_invoice" if row["target_type"] == "invoice" else "finance_expense_claim"
            return bool(await conn.fetchval(f"SELECT EXISTS(SELECT 1 FROM {table} WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3 AND owner_id=$4)", who.tenant_id, row["legal_entity_id"], row["target_id"], who.user_id))
        if name in {"audits", "supplements"}:
            parent = await conn.fetchrow("SELECT * FROM finance_archive WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3", who.tenant_id, row["legal_entity_id"], row["archive_id"])
            return parent is not None and await self._f3_visible(conn, who, parent, "archives")
        if name != "plans":
            return False
        return bool(await conn.fetchval("SELECT EXISTS(SELECT 1 FROM finance_compliance_workflow w JOIN finance_compliance_workflow_step s ON(s.tenant_id=w.tenant_id AND s.legal_entity_id=w.legal_entity_id AND s.workflow_id=w.id) JOIN finance_grant_epoch e ON(e.tenant_id=s.tenant_id AND e.legal_entity_id=s.legal_entity_id AND e.user_id=s.approver_user_id AND e.generation=s.authority_generation AND e.role IN ('approver','reviewer')) WHERE w.tenant_id=$1 AND w.legal_entity_id=$2 AND w.plan_id=$3 AND s.approver_user_id=$4)", who.tenant_id, row["legal_entity_id"], row["id"], who.user_id))

    async def _f3_record_view(self, conn: Connection, who: FinancePrincipal, row: Any, name: str) -> dict[str, Any]:
        """序列化领域事实，不包含原件对象路径、内部授权或凭据。"""
        if name == "plans":
            return await self._f3_plan_view(conn, who, row)
        hidden = {"tenant_id", "owner_id", "raw"}
        result = {k: (v.isoformat() if hasattr(v, "isoformat") else decode(v) if k in {"data", "snapshot"} and v is not None else v) for k, v in dict(row).items() if k not in hidden}
        if name == "red_cases":
            case = result["data"]
            deadline = min(datetime.fromisoformat(case["initiated_at"]) + timedelta(hours=72), datetime.fromisoformat(case["authority_deadline"]))
            result["effective_deadline"] = deadline.isoformat()
            result["display_status"] = "expired_pending_check" if row["status"] == "awaiting_confirmation" and datetime.now(UTC) >= deadline else row["status"]
        if name == "archives":
            health = await conn.fetchrow("SELECT supplement_required,last_audit_status FROM finance_archive_health WHERE tenant_id=$1 AND legal_entity_id=$2 AND archive_id=$3", row["tenant_id"], row["legal_entity_id"], row["id"])
            result.update(dict(health) if health is not None else {"supplement_required": True, "last_audit_status": None})
            snapshot = decode(row["snapshot"])
            changes = await archive_source_changes(conn, row)
            result["supplement_event_ids"] = [e["id"] for e in changes]
            result["supplement_events"] = changes
            result["supplement_required"] = result["supplement_required"] or bool(changes)
            result["snapshot"] = public_archive_snapshot(snapshot)
        return result

    async def _f3_get(self, who: FinancePrincipal, name: str, id: str) -> dict[str, Any]:
        """固定受控表的单记录权限与列表一致。"""
        table = TABLES[name]
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(f"SELECT * FROM {table} WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
            if row is None or not await self._f3_visible(conn, who, row, name):
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "合规记录不存在")
            return await self._f3_record_view(conn, who, row, name)

    async def _f3_list(self, who: FinancePrincipal, name: str, company: str | None, cursor: str | None, limit: int, *, parent_id: str | None = None) -> dict[str, Any]:
        """所有列表在权限过滤之后计数，再进行稳定键集分页。"""
        table = TABLES[name]
        scope = request_digest([who.tenant_id, who.user_id, name, company, parent_id])
        stamp, last = timestamp_cursor(cursor, scope, limit)
        async with self.pool.acquire() as conn, conn.transaction(isolation="repeatable_read"):
            if company is not None:
                await self._role(conn, who, company)
            parent_field = "invoice_id" if name == "verifications" else "archive_id"
            where_parent = f" AND r.{parent_field}=$3" if parent_id is not None else " AND $3::text IS NULL"
            condition = ("r.tenant_id=$1 AND ($2::text IS NULL OR r.legal_entity_id=$2)"
                         + where_parent + " AND " + visibility(name))
            args = (who.tenant_id, company, parent_id, who.user_id)
            total = await conn.fetchval(f"SELECT count(*) FROM {table} r WHERE {condition}", *args)
            selected = await conn.fetch(
                f"SELECT r.* FROM {table} r WHERE {condition} "
                "AND ($5::timestamptz IS NULL OR (r.created_at,r.id)>($5,$6::text)) "
                "ORDER BY r.created_at,r.id LIMIT $7", *args, stamp, last, limit + 1)
            items = [await self._f3_record_view(conn, who, r, name) for r in selected[:limit]]
            next_cursor = encode_cursor(scope, [selected[limit - 1]["created_at"].isoformat(), selected[limit - 1]["id"]]) if len(selected) > limit else None
            return page(items, total, next_cursor)

    async def get_operation(self, who: FinancePrincipal, id: str) -> dict[str, Any]:
        """F3任务按前缀恢复，旧任务继续原实现。"""
        if not id.startswith("f3op_"):
            return await super().get_operation(who, id)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "合规任务不存在")
            await self._f3_plan_row(conn, who, row["plan_id"])
            return self._f3_operation_view(row)

    async def cancel_operation(self, who: FinancePrincipal, id: str, expected_version: int, key: str) -> dict[str, Any]:
        """未发送可以取消，发送后取消只进入原请求核对。"""
        if not id.startswith("f3op_"):
            return await super().cancel_operation(who, id, expected_version, key)
        return await self._f3_control(who, id, expected_version, key, reconcile=False)

    async def reconcile_operation(self, who: FinancePrincipal, id: str, expected_version: int, key: str) -> dict[str, Any]:
        """人工核对始终只调度原请求GET，不能人工编造业务成功。"""
        if not id.startswith("f3op_"):
            return await super().reconcile_operation(who, id, expected_version, key)
        return await self._f3_control(who, id, expected_version, key, reconcile=True)

    async def _f3_control(self, who: FinancePrincipal, id: str, version: int, key: str, *, reconcile: bool) -> dict[str, Any]:
        """人工控制和发送使用同样目标锁序与CAS。"""
        if who.origin != "human":
            raise FinanceError("HUMAN_REQUIRED", 403, "合规取消和人工核对必须由真人决定")
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
            if row is None:
                raise FinanceError("RESOURCE_NOT_FOUND", 404, "合规任务不存在")
            await self._f3_plan_row(conn, who, row["plan_id"])
            await self._f3_lock_subject(conn, who.tenant_id, row["legal_entity_id"], row["target_type"], row["target_id"])
            request = {"id": id, "version": version}
            name = "reconcileComplianceOperation" if reconcile else "cancelComplianceOperation"
            old = await self._business_previous(conn, who, row["legal_entity_id"], name, key, request)
            if old is not None:
                return old
            row = await conn.fetchrow("SELECT * FROM finance_compliance_operation WHERE tenant_id=$1 AND id=$2 FOR UPDATE", who.tenant_id, id)
            assert row is not None
            if row["owner_id"] != who.user_id and await self._role(conn, who, row["legal_entity_id"]) != "reviewer":
                raise FinanceError("EXECUTOR_FORBIDDEN", 403, "仅原经办或当前reviewer可以控制合规任务")
            if row["version"] != version:
                raise FinanceError("VERSION_CONFLICT", 409, "合规任务版本已变化")
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                raise FinanceError("OPERATION_TERMINAL", 409, "已确认结果不能人工改写")
            if reconcile and row["request"] is None:
                raise FinanceError("RECONCILE_NOT_SENT", 409, "尚未发送不能查单")
            sent = row["request"] is not None
            row = await conn.fetchrow("UPDATE finance_compliance_operation SET status=$3,version=version+1,query_requested=$4,next_query_at=CASE WHEN $4 THEN now() ELSE NULL END,lease_token=NULL,lease_until=NULL WHERE tenant_id=$1 AND id=$2 RETURNING *", who.tenant_id, id, "outcome_unknown" if sent else "cancelled", sent)
            assert row is not None
            return await self._business_remember(conn, who, row["legal_entity_id"], name, key, request, self._f3_operation_view(row))
