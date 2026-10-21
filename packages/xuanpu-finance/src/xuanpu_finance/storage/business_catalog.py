"""不可变制度版本与按员工范围过滤的受控目录。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest
from xuanpu_finance.storage.business_base import BusinessBase
from xuanpu_finance.storage.common import decode, decode_cursor, encode_cursor, page

if TYPE_CHECKING:
    from xuanpu_finance.expenses.models import ExpenseDraft, ExpensePolicy
    from xuanpu_finance.models import FinancePrincipal
    from xuanpu_finance.storage.business_base import Connection


class CatalogStore(BusinessBase):
    """目录和制度由服务配置显式注入，不接受员工自报制度。"""

    async def configure_expenses(self, policies: list[ExpensePolicy]) -> None:
        """事务替换当前制度，旧版本和历史业务记录永久保留。"""
        scopes = {(policy.tenant_id, policy.legal_entity_id) for policy in policies}
        if len(scopes) != len(policies):
            raise FinanceError("CONFIGURATION_INVALID", 422, "公司制度重复")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(617472004)")
            for policy in policies:
                await self._validate_policy_roles(conn, policy)
                body = policy.model_dump(mode="json")
                digest = request_digest(body)
                previous = await conn.fetchrow(
                    "SELECT * FROM finance_expense_policy_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND version=$3",
                    policy.tenant_id,
                    policy.legal_entity_id,
                    policy.version,
                )
                if previous is not None and previous["digest"] != digest:
                    raise FinanceError("POLICY_VERSION_CONFLICT", 409, "制度同版本内容不可变")
                await conn.execute(
                    "INSERT INTO finance_expense_policy_revision VALUES($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING",
                    policy.tenant_id,
                    policy.legal_entity_id,
                    policy.version,
                    digest,
                    policy.model_dump_json(),
                )
            existing = await conn.fetch(
                "SELECT tenant_id,legal_entity_id FROM finance_expense_policy WHERE enabled"
            )
            for current in existing:
                if (current["tenant_id"], current["legal_entity_id"]) not in scopes:
                    await conn.execute(
                        "UPDATE finance_expense_policy SET enabled=false,generation=generation+1 WHERE tenant_id=$1 AND legal_entity_id=$2",
                        current["tenant_id"],
                        current["legal_entity_id"],
                    )

            for policy in policies:
                await conn.execute(
                    "INSERT INTO finance_expense_policy(tenant_id,legal_entity_id,version,enabled) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,legal_entity_id) DO UPDATE SET version=EXCLUDED.version,enabled=EXCLUDED.enabled,generation=CASE WHEN finance_expense_policy.version<>EXCLUDED.version OR finance_expense_policy.enabled<>EXCLUDED.enabled THEN finance_expense_policy.generation+1 ELSE finance_expense_policy.generation END",
                    policy.tenant_id,
                    policy.legal_entity_id,
                    policy.version,
                    policy.enabled,
                )
            await conn.execute(
                "UPDATE finance_execution_grant g SET active=false WHERE active AND scope='expense_approved' AND NOT EXISTS(SELECT 1 FROM finance_expense_policy p JOIN finance_expense_policy_revision r USING(tenant_id,legal_entity_id,version) WHERE p.tenant_id=g.tenant_id AND p.legal_entity_id=g.legal_entity_id AND p.enabled AND p.version=g.policy_version AND r.digest=g.policy_digest AND p.generation=g.policy_generation)"
            )

    async def _validate_policy_roles(self, conn: Connection, policy: ExpensePolicy) -> None:
        """启用制度必须有真实公司、不同且当前有效的审批和异常人员。"""
        company = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM finance_company WHERE tenant_id=$1 AND legal_entity_id=$2)",
            policy.tenant_id,
            policy.legal_entity_id,
        )
        if not company:
            raise FinanceError("CONFIGURATION_INVALID", 422, "制度公司不存在")
        if not policy.enabled:
            return
        for step in policy.steps:
            role = await conn.fetchval(
                "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE",
                policy.tenant_id,
                policy.legal_entity_id,
                step.approver_user_id,
            )
            if role not in {"approver", "reviewer"}:
                raise FinanceError("CONFIGURATION_INVALID", 422, "审批人缺少当前公司审批授权")
        role = await conn.fetchval(
            "SELECT role FROM finance_grant WHERE tenant_id=$1 AND legal_entity_id=$2 AND user_id=$3 FOR SHARE",
            policy.tenant_id,
            policy.legal_entity_id,
            policy.exception_owner_user_id,
        )
        if role != "reviewer":
            raise FinanceError("CONFIGURATION_INVALID", 422, "异常负责人必须为当前reviewer")

    async def _policy(
        self, conn: Connection, tenant: str, entity: str, *, require: bool = True
    ) -> dict[str, Any] | None:
        """读取并锁住当前制度，避免执行期间撤权或替换。"""
        current = await conn.fetchrow(
            "SELECT * FROM finance_expense_policy WHERE tenant_id=$1 AND legal_entity_id=$2 FOR SHARE",
            tenant,
            entity,
        )
        if current is None or not current["enabled"]:
            if require:
                raise FinanceError("EXPENSE_POLICY_REQUIRED", 409, "公司未启用报销制度")
            return None
        row = await conn.fetchrow(
            "SELECT * FROM finance_expense_policy_revision WHERE tenant_id=$1 AND legal_entity_id=$2 AND version=$3",
            tenant,
            entity,
            current["version"],
        )
        assert row is not None
        return {**decode(row["data"]), "digest": row["digest"], "generation": current["generation"]}

    def _validate_draft(
        self, who: FinancePrincipal, draft: ExpenseDraft, policy: dict[str, Any]
    ) -> None:
        """分类、归属必须来自当前制度且员工确实可用。"""
        categories = {entry["id"] for entry in policy["categories"]}
        centers = {(entry["id"], entry["kind"]): entry for entry in policy["cost_centers"]}
        total = sum((Decimal(line.amount) for line in draft.lines), Decimal(0))
        if total > Decimal(policy["max_claim_amount"]):
            raise FinanceError("CLAIM_AMOUNT_LIMIT", 409, "报销总额超过制度限额")
        for line in draft.lines:
            if line.category_id not in categories:
                raise FinanceError("CATEGORY_INVALID", 422, "费用分类不在当前目录")
            provided = [(line.department_id, "department"), (line.project_id, "project")]
            if policy["require_cost_center"] and not any(value for value, _ in provided):
                raise FinanceError("COST_CENTER_REQUIRED", 422, "费用必须选择归属")
            for value, kind in provided:
                if value is None:
                    continue
                center = centers.get((value, kind))
                if center is None or who.user_id not in center["allowed_user_ids"]:
                    raise FinanceError("COST_CENTER_FORBIDDEN", 403, "费用归属未授权给当前员工")

    async def list_expense_categories(
        self, who: FinancePrincipal, entity: str, cursor: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """列出当前生效制度分类，空制度返回空目录。"""
        return await self._catalog_page(who, entity, "categories", cursor, limit)

    async def list_cost_centers(
        self,
        who: FinancePrincipal,
        entity: str,
        cursor: str | None = None,
        limit: int = 50,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """只列出员工明确有权选择的部门或项目。"""
        if kind not in {None, "department", "project"}:
            raise FinanceError("COST_CENTER_KIND_INVALID", 422, "归属类型无效")
        return await self._catalog_page(who, entity, "cost_centers", cursor, limit, kind)

    async def _catalog_page(
        self,
        who: FinancePrincipal,
        entity: str,
        kind: str,
        cursor: str | None,
        limit: int,
        filter_kind: str | None = None,
    ) -> dict[str, Any]:
        """目录按ID稳定续读并在筛选后独立计数。"""
        scope = request_digest([who.tenant_id, who.user_id, entity, kind, filter_kind])
        after = decode_cursor(cursor, scope, limit)
        if after is not None and len(after) != 1:
            raise FinanceError("CURSOR_INVALID", 400, "目录游标无效")
        async with self.pool.acquire() as conn, conn.transaction():
            await self._role(conn, who, entity)
            policy = await self._policy(conn, who.tenant_id, entity, require=False)
            items = [] if policy is None else policy[kind]
            if kind == "cost_centers":
                items = [
                    {key: entry[key] for key in ("id", "name", "kind")}
                    for entry in items
                    if who.user_id in entry["allowed_user_ids"]
                    and (filter_kind is None or entry["kind"] == filter_kind)
                ]
            elif policy is not None:
                items = [
                    {"id": entry["id"], "name": entry["name"], "policy_version": policy["version"]}
                    for entry in items
                ]
            items = sorted(items, key=lambda item: item["id"])
            remaining = [entry for entry in items if after is None or entry["id"] > after[0]]
            visible = remaining[:limit]
            cursor_next = (
                encode_cursor(scope, [visible[-1]["id"]]) if len(remaining) > limit else None
            )
            return page(visible, len(items), cursor_next)
