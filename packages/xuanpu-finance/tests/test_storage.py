"""验证真实数据库幂等、业务授权与持久上下文防重放。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FileRef, FinancePrincipal
from xuanpu_finance.storage import FinanceStore

WHO = FinancePrincipal("tenant-a", "user-a")
FILE = FileRef(file_id="f", revision=1, sha256="0" * 64)


async def prepare(store: FinanceStore) -> None:
    """建立本服务自己的公司授权。"""
    await store.seed_company(WHO.tenant_id, "company-a", "A公司", "TAX-A")
    await store.grant(WHO.tenant_id, WHO.user_id, "company-a")


async def test_import_idempotency_concurrency_and_cancel(store: FinanceStore) -> None:
    """并发相同请求仅受理一次；取消和版本检查保持一致。"""
    await prepare(store)
    results = await asyncio.gather(
        *(store.accept_import(WHO, "company-a", [FILE], "same") for _ in range(8))
    )
    assert len({result["id"] for result in results}) == 1
    operation = results[0]
    assert operation["summary"]["pending"] == 1
    with pytest.raises(FinanceError, match="IDEMPOTENCY_CONFLICT"):
        await store.accept_import(
            WHO, "company-a", [FILE.model_copy(update={"file_id": "other"})], "same"
        )
    cancelled = await store.cancel_operation(WHO, operation["id"], 1, "cancel")
    assert cancelled["status"] == "cancelled"
    assert cancelled["summary"]["cancelled"] == 1
    assert await store.cancel_operation(WHO, operation["id"], 1, "cancel") == cancelled


async def test_tenant_company_owner_and_reviewer_isolation(store: FinanceStore) -> None:
    """平台 admin 无业务权限，普通授权人只能看本人操作。"""
    await prepare(store)
    operation = await store.accept_import(WHO, "company-a", [FILE], "one")
    for outsider in (
        FinancePrincipal("tenant-b", "user-a"),
        FinancePrincipal("tenant-a", "platform-admin"),
    ):
        with pytest.raises(FinanceError):
            await store.get_operation(outsider, operation["id"])
    await store.grant("tenant-a", "user-b", "company-a")
    with pytest.raises(FinanceError):
        await store.get_operation(FinancePrincipal("tenant-a", "user-b"), operation["id"])
    await store.grant("tenant-a", "reviewer", "company-a", "reviewer")
    assert (await store.get_operation(FinancePrincipal("tenant-a", "reviewer"), operation["id"]))[
        "id"
    ] == operation["id"]
    assert (await store.list_companies(WHO))["total"] == 1
    assert all(
        item["status"] == "unconfigured"
        for item in (await store.list_integrations(WHO, "company-a"))["items"]
    )


async def test_persistent_context_replay_claim_is_atomic(store: FinanceStore) -> None:
    """同上下文同幂等键重试可用，换内容、身份、键一律拒绝。"""
    expiry = datetime.now(UTC) + timedelta(minutes=1)
    await asyncio.gather(
        *(store.claim_context("issuer", "jti", "subject", "hash", "key", expiry) for _ in range(8))
    )
    await FinanceStore(store.pool).claim_context("issuer", "jti", "subject", "hash", "key", expiry)
    for subject, request_hash, key in (
        ("other", "hash", "key"),
        ("subject", "different", "key"),
        ("subject", "hash", "different"),
    ):
        with pytest.raises(FinanceError, match="CONTEXT_REPLAY"):
            await store.claim_context("issuer", "jti", subject, request_hash, key, expiry)
    with pytest.raises(FinanceError, match="CONTEXT_EXPIRED"):
        await store.claim_context(
            "issuer", "expired", "subject", "hash", "key", datetime.now(UTC) - timedelta(seconds=1)
        )


async def test_configure_removes_revoked_grants_without_deleting_history(
    store: FinanceStore,
) -> None:
    """启动配置移除授权必须实际撤权，业务历史保留。"""
    await prepare(store)
    operation = await store.accept_import(WHO, "company-a", [FILE], "history")
    await store.configure(
        [
            {
                "tenant_id": "tenant-a",
                "legal_entity_id": "company-a",
                "name": "A公司",
                "tax_id": "TAX-A",
            }
        ],
        [],
    )
    with pytest.raises(FinanceError):
        await store.check_access(WHO, "company-a")
    assert (
        await store.pool.fetchval(
            "SELECT count(*) FROM finance_operation WHERE id=$1", operation["id"]
        )
        == 1
    )
    with pytest.raises(FinanceError, match="CONFIGURATION_INVALID"):
        await store.configure(
            [],
            [
                {
                    "tenant_id": "other",
                    "user_id": "user",
                    "legal_entity_id": "missing",
                    "role": "collector",
                }
            ],
        )


async def test_keyset_pagination_survives_new_records(store: FinanceStore) -> None:
    """翻页期间新增排序更靠前的标识，不应重项或跳过旧记录。"""
    for entity in ("b", "d", "f"):
        await store.seed_company(WHO.tenant_id, entity, entity, "tax")
        await store.grant(WHO.tenant_id, WHO.user_id, entity)
    first = await store.list_companies(WHO, limit=1)
    assert first["total"] == 3
    await store.seed_company(WHO.tenant_id, "a", "new", "tax")
    await store.grant(WHO.tenant_id, WHO.user_id, "a")
    seen = [item["id"] for item in first["items"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        following = await store.list_companies(WHO, cursor, 1)
        assert following["total"] == 4
        seen.extend(item["id"] for item in following["items"])
        cursor = following["next_cursor"]
    assert len(seen) == len(set(seen)) == 4 and set(seen) == {"a", "b", "d", "f"}


async def test_cursor_malformed_or_wrong_scope_is_bad_request(store: FinanceStore) -> None:
    """非法游标和跨查询复用游标明确返回400。"""
    await prepare(store)
    await store.seed_company(WHO.tenant_id, "other", "Other", "TAX")
    await store.grant(WHO.tenant_id, WHO.user_id, "other")
    cursor = (await store.list_companies(WHO, limit=1))["next_cursor"]
    for invalid in ("not-a-cursor", "0", cursor):
        with pytest.raises(FinanceError) as error:
            await store.list_invoices(WHO, "company-a", invalid, 1)
        assert error.value.status == 400
