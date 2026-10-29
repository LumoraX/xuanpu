"""独立合规目标授权、统一锁序与实时业务资格绑定。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.compliance.archive_checks import archive_profile_eligible
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.common import decode
from xuanpu_finance.storage.f3_catalog import ComplianceCatalogStore
from xuanpu_finance.storage.f3_evidence_closure import expand_invoice_ids
from xuanpu_finance.storage.f3_projection import invoice_projection, require_usable_projection
from xuanpu_finance.storage.f5_archive import funding_family

if TYPE_CHECKING:
    from xuanpu_finance.compliance.models import ComplianceActionPreview
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class ComplianceTargetStore(ComplianceCatalogStore):
    """需要单据时先锁单据，然后排序蓝票，最后锁合规状态。"""

    async def _f3_invoice(self, conn: Connection, who: FinancePrincipal, id: str, *, lock: bool = False) -> Any:
        """租户和实际指派控制必要证据读取，写入先取得票据行锁。"""
        row = await conn.fetchrow("SELECT * FROM finance_invoice WHERE tenant_id=$1 AND id=$2" + (" FOR UPDATE" if lock else ""), who.tenant_id, id)
        return await self._authorize_invoice(conn, who, row)

    async def _f3_document(self, conn: Connection, who: FinancePrincipal, id: str, *, lock: bool = False) -> Any:
        """归档审批人员仅取得实际指派目标单据的必要证据读取权。"""
        row = await conn.fetchrow("SELECT * FROM finance_expense_claim WHERE tenant_id=$1 AND id=$2" + (" FOR UPDATE" if lock else ""), who.tenant_id, id)
        if row is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "归档单据不存在")
        role = await self._role(conn, who, row["legal_entity_id"])
        if role == "reviewer" or row["owner_id"] == who.user_id:
            return row
        if await self._f3_assigned(conn, who, row["legal_entity_id"], row["document_kind"], id):
            return row
        return await self._claim_row(conn, who, id)

    async def _f3_state(self, conn: Connection, invoice: Any, *, lock: bool = False) -> dict[str, Any]:
        """内容版本和合规版本分开，初始状态不伪造权威查验。"""
        scope = (invoice["tenant_id"], invoice["legal_entity_id"], invoice["id"])
        if lock:
            await conn.execute("INSERT INTO finance_invoice_compliance(tenant_id,legal_entity_id,invoice_id) VALUES($1,$2,$3) ON CONFLICT DO NOTHING", *scope)
        row = await conn.fetchrow("SELECT * FROM finance_invoice_compliance WHERE tenant_id=$1 AND legal_entity_id=$2 AND invoice_id=$3" + (" FOR UPDATE" if lock else ""), *scope)
        if row is None:
            return {"version": 1, "authority_revision": 0, "verification_status": "not_checked", "invoice_status": decode(invoice["data"])["ticket_status"], "snapshot": None, "valid_until": None}
        return {**dict(row), "snapshot": decode(row["snapshot"]) if row["snapshot"] is not None else None, "valid_until": row["valid_until"].isoformat() if row["valid_until"] is not None else None}

    async def _f3_target(self, conn: Connection, who: FinancePrincipal, kind: str, id: str, *, lock: bool = False) -> dict[str, Any]:
        """为四种实际目标形成独立描述，不伪造报销外键。"""
        if kind == "invoice":
            base = await self._f3_invoice(conn, who, id)
            ids = await expand_invoice_ids(conn, who.tenant_id, base["legal_entity_id"], [id])
            rows = [await self._f3_invoice(conn, who, invoice_id, lock=lock) for invoice_id in ids]
            states = {r["id"]: await self._f3_state(conn, r, lock=lock) for r in rows}
            row = next(r for r in rows if r["id"] == id)
            data, state = decode(row["data"]), states[id]
            reds = [{"id": r["id"], "data": decode(r["data"]), "compliance": states[r["id"]], "simulation": r["source_simulation"]} for r in rows if r["id"] != id]
            return {"id": id, "type": kind, "tenant_id": row["tenant_id"], "legal_entity_id": row["legal_entity_id"], "owner_id": row["owner_id"], "version": state["version"], "content_version": data["version"], "compliance_version": state["version"], "simulation": row["source_simulation"], "data": data, "compliance": state, "linked_red_invoices": reds}
        if kind in {"expense_claim", "payable", "funding_case"}:
            row = await self._f3_document(conn, who, id, lock=lock)
            if row["document_kind"] != kind:
                raise FinanceError("TARGET_KIND_MISMATCH", 422, "归档单据类型不符")
            documents = await funding_family(conn, who.tenant_id, row["legal_entity_id"], [id]) if kind == "funding_case" else [id]
            links = await conn.fetch("SELECT DISTINCT invoice_id FROM finance_allocation WHERE tenant_id=$1 AND legal_entity_id=$2 AND document_id=ANY($3::text[]) AND state IN ('reserved','consumed') ORDER BY invoice_id", who.tenant_id, row["legal_entity_id"], documents)
            invoices = []
            ids = await expand_invoice_ids(conn, who.tenant_id, row["legal_entity_id"], [link["invoice_id"] for link in links])
            invoice_rows = [await self._f3_invoice(conn, who, invoice_id, lock=lock) for invoice_id in ids]
            for invoice in invoice_rows:
                state = await self._f3_state(conn, invoice, lock=lock)
                invoices.append({"id": invoice["id"], "data": decode(invoice["data"]), "compliance": state, "simulation": invoice["source_simulation"]})
            return {"id": id, "type": kind, "tenant_id": row["tenant_id"], "legal_entity_id": row["legal_entity_id"], "owner_id": row["owner_id"], "version": row["version"], "compliance_version": max((i["compliance"]["version"] for i in invoices), default=1), "simulation": row["simulation"], "data": decode(row["content"]), "amount": format(row["total"], ".2f"), "invoices": invoices}
        table = "finance_red_letter_case" if kind == "red_letter_case" else "finance_archive" if kind == "archive" else None
        if table is None:
            raise FinanceError("TARGET_KIND_MISMATCH", 422, "合规目标类型无效")
        current = await conn.fetchrow(f"SELECT * FROM {table} WHERE tenant_id=$1 AND id=$2", who.tenant_id, id)
        if current is None:
            raise FinanceError("RESOURCE_NOT_FOUND", 404, "合规目标不存在")
        if kind == "red_letter_case":
            parent = await self._f3_target(conn, who, "invoice", current["invoice_id"], lock=lock)
        else:
            parent = await self._f3_target(conn, who, current["target_type"], current["target_id"], lock=lock)
        row = await conn.fetchrow(f"SELECT * FROM {table} WHERE tenant_id=$1 AND legal_entity_id=$2 AND id=$3" + (" FOR UPDATE" if lock and kind != "archive" else ""), who.tenant_id, current["legal_entity_id"], id)
        assert row is not None
        return {"id": id, "type": kind, "tenant_id": row["tenant_id"], "legal_entity_id": row["legal_entity_id"], "owner_id": row["owner_id"], "version": row["version"], "compliance_version": parent["compliance_version"], "simulation": parent["simulation"], "data": decode(row["data"]), "parent": parent, "manifest_hash": row["manifest_hash"] if kind == "archive" else None}

    async def _f3_bindings(self, conn: Connection, who: FinancePrincipal, target: dict[str, Any], body: ComplianceActionPreview) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """每次预检和发送重新派生岗位、来源、版本和实际税务语义。"""
        if body.expected_version != target["version"]:
            raise FinanceError("VERSION_CONFLICT", 409, "合规目标版本已变化")
        policy = await self._compliance_policy(conn, who.tenant_id, target["legal_entity_id"])
        assert policy is not None
        kind = "archive" if body.action in {"archive", "audit_archive"} else "tax"
        provider = await self._compliance_provider(conn, policy, kind)
        if provider["simulation"] != target["simulation"]:
            raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "原件来源与合规渠道环境不一致")
        executors = policy[kind + "_executor_user_ids"]
        if who.user_id not in executors or await self._role(conn, who, target["legal_entity_id"]) != "reviewer":
            raise FinanceError("EXECUTOR_FORBIDDEN", 403, "必须由当前专用合规经办人准备计划")
        steps = [s for s in policy[kind + "_steps"] if Decimal(self._f3_amount(target)) >= Decimal(s["minimum_amount"])]
        if not steps or any(s["approver_user_id"] in {who.user_id, target["owner_id"]} for s in steps):
            raise FinanceError("APPROVAL_CHAIN_INVALID", 409, "合规审批链为空或包含申请人/经办本人")
        users = {who.user_id, target["owner_id"], *(s["approver_user_id"] for s in steps)}
        authorities = [dict(r) for r in await conn.fetch("SELECT user_id,role,generation FROM finance_grant_epoch WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=ANY($3::text[]) ORDER BY user_id", who.tenant_id, target["legal_entity_id"], sorted(users))]
        if len(authorities) != len(users) or any(a["role"] not in {"reviewer", "approver"} for a in authorities if a["user_id"] in {s["approver_user_id"] for s in steps}):
            raise FinanceError("APPROVER_FORBIDDEN", 409, "合规审批岗位已失效")
        extra = await self._f3_action_eligibility(conn, target, body, policy)
        return {"target": target, "policy": policy, "provider": provider, "authorities": authorities, **extra}, steps

    def _f3_amount(self, target: dict[str, Any]) -> str:
        """合规动作金额仅从持久目标推导。"""
        data = target["data"]
        return format(abs(Decimal(target.get("amount", data.get("gross", data.get("amount", "0.00"))))), ".2f")

    async def _f3_action_eligibility(self, conn: Connection, target: dict[str, Any], body: ComplianceActionPreview, policy: dict[str, Any]) -> dict[str, Any]:
        """确认仅复核已有事实，不提供开红票、建确认单或税务申报入口。"""
        if body.action == "archive":
            sources = [*target.get("invoices", []), *target.get("linked_red_invoices", [])]
            if any(source["simulation"] != target["simulation"] for source in sources):
                raise FinanceError("SIMULATION_MODE_CONFLICT", 409, "归档关联原件含不同来源环境，旧正式原件不能升级为模拟材料")
            if not archive_profile_eligible(target["simulation"], policy["archive_profile_id"], policy["archive_profile_version"], policy["required_lock_mode"]):
                raise FinanceError("ARCHIVE_PROFILE_UNVERIFIED", 409, "归档标准profile尚未验收或隔离锁不符")
            return {}
        if body.action == "audit_archive":
            return {}
        invoice = target["parent"] if target["type"] == "red_letter_case" else target
        projected = await invoice_projection(conn, target["tenant_id"], target["legal_entity_id"], invoice["data"])
        if projected["verification_status"] != "passed":
            raise FinanceError("VALID_VERIFICATION_REQUIRED", 409, "合规确认须有当前有效查验")
        if invoice["data"].get("parser_profile") not in policy["supported_invoice_profiles"]:
            raise FinanceError("INVOICE_PROFILE_UNSUPPORTED", 409, "票种尚未配置为税务可用")
        if body.action == "confirm_red_letter":
            return self._f3_red_confirmation_eligibility(target, body)
        require_usable_projection(projected)
        if invoice["data"]["is_red"] or body.parameters.accounting_period not in policy["open_periods"]:
            raise FinanceError("TAX_PERIOD_OR_INVOICE_INVALID", 409, "税务期间未开放或目标不是有效蓝票")
        snapshot = invoice["compliance"]["snapshot"]
        if snapshot is not None and snapshot["usage_status" if body.action == "confirm_usage" else "tax_entry_status"] == "confirmed":
            raise FinanceError("TAX_ALREADY_CONFIRMED", 409, "该项税务确认已经成立")
        return {"amount": projected["effective_gross"], "tax": projected["effective_tax"], "accounting_period": body.parameters.accounting_period, "usage_kind": policy["usage_kind"]}

    def _f3_red_confirmation_eligibility(self, target: dict[str, Any], body: ComplianceActionPreview) -> dict[str, Any]:
        """原确认单身份与原始72小时截止固定，不因新计划重置。"""
        case = target["data"]
        deadline = min(datetime.fromisoformat(case["initiated_at"]) + timedelta(hours=72), datetime.fromisoformat(case["authority_deadline"]))
        if case["status"] != "awaiting_confirmation" or not case["our_side"] or body.parameters.confirmation_id != case["confirmation_id"]:
            raise FinanceError("RED_CONFIRMATION_MISMATCH", 409, "仅本方待确认的原权威确认单可确认")
        if datetime.now(UTC) >= deadline:
            raise FinanceError("RED_CONFIRMATION_EXPIRED", 409, "原始红字确认时限已经届满")
        return {"confirmation_deadline": deadline.isoformat(), "amount": case["amount"], "tax": case["tax"]}
