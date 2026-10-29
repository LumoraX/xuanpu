"""财务独立 PostgreSQL 公共接口。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.storage.f1_schema import F1_SCHEMA
from xuanpu_finance.storage.f2_schema import F2_INBOX_SCHEMA, F2_SCHEMA
from xuanpu_finance.storage.f3_schema import F3_SCHEMA, immutable_schema
from xuanpu_finance.storage.f4_query_schema import F4_QUERY_SCHEMA
from xuanpu_finance.storage.f4_schema import F4_SCHEMA
from xuanpu_finance.storage.f5_schema import F5_SCHEMA
from xuanpu_finance.storage.f10_readiness import InternalReadinessStore
from xuanpu_finance.storage.schema import SCHEMA

if TYPE_CHECKING:
    from xuanpu_finance.models import FinancePrincipal


class FinanceStore(InternalReadinessStore):
    """显式注入连接池、全部业务状态独立持久化的财务存储。"""

    async def migrate(self) -> None:
        """用事务锁串行执行幂等 F0 建表。"""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(617472001)")
            await conn.execute(SCHEMA)
            await conn.execute(F1_SCHEMA)
            await conn.execute(F2_SCHEMA)
            await conn.execute(F2_INBOX_SCHEMA)
            await conn.execute(F3_SCHEMA)
            await conn.execute(F4_SCHEMA)
            await conn.execute(F4_QUERY_SCHEMA)
            await conn.execute(immutable_schema())
            await conn.execute(F5_SCHEMA)

    async def seed_company(
        self, tenant_id: str, legal_entity_id: str, name: str, tax_id: str
    ) -> None:
        """由显式运维入口配置本服务公司的基本资料。"""
        await self.pool.execute(
            "INSERT INTO finance_company(tenant_id,legal_entity_id,name,tax_id) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,legal_entity_id) DO UPDATE SET name=EXCLUDED.name,tax_id=EXCLUDED.tax_id",
            tenant_id,
            legal_entity_id,
            name,
            tax_id,
        )

    async def grant(
        self, tenant_id: str, user_id: str, legal_entity_id: str, role: str = "collector"
    ) -> None:
        """配置公司内最小角色，禁止隐式平台管理员授权。"""
        if role not in {"collector", "reviewer", "approver"}:
            raise FinanceError("ROLE_INVALID", 422, "仅支持 collector/reviewer/approver")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO finance_grant VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,user_id,legal_entity_id) DO UPDATE SET role=EXCLUDED.role",
                tenant_id,
                user_id,
                legal_entity_id,
                role,
            )
            await self._sync_grant_epochs(conn)

    async def check_access(self, who: FinancePrincipal, legal_entity_id: str) -> None:
        """验证当前用户的显式公司授权。"""
        async with self.pool.acquire() as conn:
            await self._role(conn, who, legal_entity_id)

    async def claim_context(
        self,
        issuer: str,
        jti: str,
        subject: str,
        request_hash: str,
        idempotency_key: str | None,
        expires_at: float | datetime,
    ) -> None:
        """原子占用签名上下文，跨进程仅允许完全一致的重试。"""
        if isinstance(expires_at, (float, int)):
            expires_at = datetime.fromtimestamp(expires_at, UTC)
        if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
            raise FinanceError("CONTEXT_EXPIRED", 401, "上下文已过期")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM finance_context_replay WHERE expires_at < now()")
            await conn.execute(
                "INSERT INTO finance_context_replay VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING",
                issuer,
                jti,
                subject,
                request_hash,
                idempotency_key,
                expires_at,
            )
            row = await conn.fetchrow(
                "SELECT * FROM finance_context_replay WHERE issuer=$1 AND jti=$2 FOR UPDATE",
                issuer,
                jti,
            )
            if row is None or (row["subject"], row["request_hash"], row["idempotency_key"]) != (
                subject,
                request_hash,
                idempotency_key,
            ):
                raise FinanceError("CONTEXT_REPLAY", 401, "上下文已用于其他请求")

    async def configure(
        self, companies: list[dict[str, str]], grants: list[dict[str, str]]
    ) -> None:
        """独占运维启动时事务替换本服务全部角色，不删除业务及历史公司。"""
        company_keys = {
            (entry.get("tenant_id"), entry.get("legal_entity_id")) for entry in companies
        }
        grant_keys = {
            (entry.get("tenant_id"), entry.get("user_id"), entry.get("legal_entity_id"))
            for entry in grants
        }
        valid_companies = all(
            set(entry) == {"tenant_id", "legal_entity_id", "name", "tax_id"}
            and all(isinstance(value, str) and 0 < len(value) <= 256 for value in entry.values())
            for entry in companies
        )
        valid_grants = all(
            set(entry) == {"tenant_id", "user_id", "legal_entity_id", "role"}
            and entry["role"] in {"collector", "reviewer", "approver"}
            and (entry["tenant_id"], entry["legal_entity_id"]) in company_keys
            and all(isinstance(value, str) and 0 < len(value) <= 128 for value in entry.values())
            for entry in grants
        )
        if (
            not valid_companies
            or not valid_grants
            or len(company_keys) != len(companies)
            or len(grant_keys) != len(grants)
        ):
            raise FinanceError("CONFIGURATION_INVALID", 422, "公司、角色配置无效或引用未配置公司")
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(617472003)")
            await conn.executemany(
                "INSERT INTO finance_company(tenant_id,legal_entity_id,name,tax_id) VALUES($1,$2,$3,$4) ON CONFLICT(tenant_id,legal_entity_id) DO UPDATE SET name=EXCLUDED.name,tax_id=EXCLUDED.tax_id",
                [
                    (entry["tenant_id"], entry["legal_entity_id"], entry["name"], entry["tax_id"])
                    for entry in companies
                ],
            )
            await conn.execute("DELETE FROM finance_grant")
            await conn.executemany(
                "INSERT INTO finance_grant VALUES($1,$2,$3,$4)",
                [
                    (entry["tenant_id"], entry["user_id"], entry["legal_entity_id"], entry["role"])
                    for entry in grants
                ],
            )
            await self._sync_grant_epochs(conn)
