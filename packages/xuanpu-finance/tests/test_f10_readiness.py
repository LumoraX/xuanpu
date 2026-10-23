"""F10内部配置在真实只读快照内核对，不产生业务执行许可。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

from .test_f7_records import configured

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore

CHECKS = [
    "company",
    "expense_policy",
    "expense_categories",
    "approval_roles",
    "collector_separation",
    "grant_epochs",
    "external_configuration",
    "simulation_disabled",
]


async def ready(store: FinanceStore) -> FinancePrincipal:
    """设置不含外部通道和模拟的有效内部公司。"""
    await configured(store)
    await store.pool.execute(
        "INSERT INTO finance_settlement_mode(singleton,simulation) VALUES(true,false) ON CONFLICT(singleton) DO UPDATE SET simulation=false"
    )
    await store.pool.execute(
        "INSERT INTO finance_compliance_mode(singleton,simulation) VALUES(true,false) ON CONFLICT(singleton) DO UPDATE SET simulation=false"
    )
    return FinancePrincipal("t", "reviewer", "human")


def item(report: dict[str, Any], code: str) -> dict[str, Any]:
    """按固定检查代码取出单项。"""
    return next(c for c in report["checks"] if c["code"] == code)


async def test_internal_snapshot_current_grants_digest_and_redaction(store: FinanceStore) -> None:
    """公司reviewer可读；撤权恢复改变摘要，墓碑不误报且报告脱敏。"""
    reviewer = await ready(store)
    assert hasattr(store, "get_internal_records_readiness"), "缺少F10只读内部检查"
    first = await store.get_internal_records_readiness(reviewer, "e")
    assert first["status"] == first["production_status"] == "not_verified"
    assert [c["code"] for c in first["checks"]] == CHECKS
    assert all(c["status"] == "passed" for c in first["checks"])
    assert len(first["versions"]) == 1
    assert set(first["versions"][0]) == {"kind", "version", "generation", "enabled", "digest"}
    raw = json.dumps(first, ensure_ascii=False)
    for secret in ("BUYER", "approver_user_id", "exception_owner_user_id", "user_id", "snapshot"):
        assert secret not in raw
    repeated = await store.get_internal_records_readiness(reviewer, "e")
    assert repeated["configuration_digest"] == first["configuration_digest"]
    for who, entity in [
        (replace(reviewer, user_id="owner"), "e"),
        (replace(reviewer, user_id="a"), "e"),
        (replace(reviewer, tenant_id="other"), "e"),
        (reviewer, "other"),
    ]:
        with pytest.raises(FinanceError) as error:
            await store.get_internal_records_readiness(who, entity)
        assert error.value.status == 403
    await store.grant("t", "temporary", "e", "collector")
    await store.pool.execute("DELETE FROM finance_grant WHERE user_id='temporary'")
    await store._sync_grant_epochs(store.pool)
    tombstone = await store.get_internal_records_readiness(reviewer, "e")
    assert item(tombstone, "grant_epochs")["status"] == "passed"
    await store.grant("t", "a", "e", "collector")
    revoked = await store.get_internal_records_readiness(reviewer, "e")
    assert revoked["configuration_digest"] != first["configuration_digest"]
    assert item(revoked, "approval_roles")["status"] == "blocked"
    await store.grant("t", "a", "e", "approver")
    restored = await store.get_internal_records_readiness(reviewer, "e")
    assert restored["status"] == "not_verified"
    assert restored["configuration_digest"] != first["configuration_digest"]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"categories": []}, "expense_categories"),
        ({"categories": [{"id": "bad", "name": ""}]}, "expense_policy"),
        ({"require_verified_invoice": True}, "expense_policy"),
        ({"record_mode": "invoice_backed"}, "expense_policy"),
        ({"steps": []}, "expense_policy"),
    ],
)
async def test_invalid_persisted_policy_is_blocked(
    store: FinanceStore, changes: dict[str, Any], code: str
) -> None:
    """使用真实Pydantic模型拒绝畸形持久制度，空分类也不能通过。"""
    who = await ready(store)
    assert hasattr(store, "get_internal_records_readiness"), "缺少F10只读内部检查"
    await store.pool.execute("ALTER TABLE finance_expense_policy_revision DISABLE TRIGGER ALL")
    await store.pool.execute(
        "UPDATE finance_expense_policy_revision SET data=data || $1::jsonb", json.dumps(changes)
    )
    report = await store.get_internal_records_readiness(who, "e")
    assert report["status"] == item(report, code)["status"] == "blocked"


@pytest.mark.parametrize(
    "table", ["finance_settlement_policy", "finance_compliance_policy", "finance_funding_policy"]
)
async def test_enabled_orphan_external_pointer_blocks(store: FinanceStore, table: str) -> None:
    """外部当前指针没有revision仍阻断，不能用JOIN漏掉。"""
    who = await ready(store)
    assert hasattr(store, "get_internal_records_readiness"), "缺少F10只读内部检查"
    first = await store.get_internal_records_readiness(who, "e")
    await store.pool.execute(f"ALTER TABLE {table} DISABLE TRIGGER ALL")
    await store.pool.execute(
        f"INSERT INTO {table}(tenant_id,legal_entity_id,version,enabled,generation) VALUES('t','e',999,true,1)"
    )
    report = await store.get_internal_records_readiness(who, "e")
    assert item(report, "external_configuration")["status"] == "blocked"
    assert report["configuration_digest"] != first["configuration_digest"]
    await store.pool.execute(f"UPDATE {table} SET enabled=false,generation=2")
    assert (await store.get_internal_records_readiness(who, "e"))["status"] == "not_verified"


async def test_readonly_repeatable_snapshot_and_active_epoch_mismatch(
    store: FinanceStore, monkeypatch: Any
) -> None:
    """授权和检查共用readonly repeatable read事务，现行grant必须匹配epoch。"""
    who = await ready(store)
    assert hasattr(store, "get_internal_records_readiness"), "缺少F10只读内部检查"
    original = store._readiness_snapshot

    async def snapshot(conn: Any, tenant: str, entity: str) -> dict[str, Any]:
        """在真实数据库事务中核验隔离级别并复用原快照。"""
        assert await conn.fetchval("SHOW transaction_read_only") == "on"
        assert await conn.fetchval("SHOW transaction_isolation") == "repeatable read"
        return await original(conn, tenant, entity)

    monkeypatch.setattr(store, "_readiness_snapshot", snapshot)
    await store.get_internal_records_readiness(who, "e")
    await store.pool.execute("UPDATE finance_grant_epoch SET role='collector' WHERE user_id='a'")
    report = await store.get_internal_records_readiness(who, "e")
    assert item(report, "grant_epochs")["status"] == "blocked"


@pytest.mark.parametrize("changes", [{"tenant_id": "other"}, {"legal_entity_id": "other"}])
async def test_policy_body_scope_cannot_differ_from_current_pointer(
    store: FinanceStore, changes: dict[str, Any]
) -> None:
    """制度正文即使结构有效也必须属于当前租户和公司。"""
    who = await ready(store)
    await store.pool.execute("ALTER TABLE finance_expense_policy_revision DISABLE TRIGGER ALL")
    await store.pool.execute(
        "UPDATE finance_expense_policy_revision SET data=data || $1::jsonb", json.dumps(changes)
    )
    report = await store.get_internal_records_readiness(who, "e")
    assert item(report, "expense_policy")["status"] == "blocked"


@pytest.mark.parametrize(
    "table,columns,values",
    [
        (
            "finance_settlement_provider",
            "provider,configuration_version,tenant_id,legal_entity_id,digest,data,enabled,generation",
            "'private-provider',1,'t','e','private-digest','{}',true,1",
        ),
        (
            "finance_compliance_provider",
            "provider,configuration_version,tenant_id,legal_entity_id,kind,digest,data,enabled,generation",
            "'private-provider',1,'t','e','tax','private-digest','{}',true,1",
        ),
        (
            "finance_settlement_catalog",
            "tenant_id,legal_entity_id,kind,source,id,version,enabled,generation",
            "'t','e','supplier_account','private-source','private-id','1',true,1",
        ),
    ],
)
async def test_enabled_external_channel_and_orphan_catalog_block(
    store: FinanceStore, table: str, columns: str, values: str
) -> None:
    """所有外部渠道和没有revision的目录当前启用状态都直接阻断且不外泄。"""
    who = await ready(store)
    first = await store.get_internal_records_readiness(who, "e")
    await store.pool.execute(f"INSERT INTO {table}({columns}) VALUES({values})")
    report = await store.get_internal_records_readiness(who, "e")
    assert item(report, "external_configuration")["status"] == "blocked"
    assert report["configuration_digest"] != first["configuration_digest"]
    assert "private-" not in json.dumps(report)
    await store.pool.execute(f"UPDATE {table} SET enabled=false,generation=2")
    assert (await store.get_internal_records_readiness(who, "e"))["status"] == "not_verified"


async def test_scope_digest_disabled_expense_and_query_failure(
    store: FinanceStore, monkeypatch: Any
) -> None:
    """摘要绑定公司，制度停用阻断，数据库快照失败不能返回部分成功。"""
    from .test_expense_domain import policy

    who = await ready(store)
    one = await store.get_internal_records_readiness(who, "e")
    await store.seed_company("t", "other", "公司", "BUYER")
    for user, role in [
        ("owner", "collector"),
        ("a", "approver"),
        ("b", "approver"),
        ("reviewer", "reviewer"),
    ]:
        await store.grant("t", user, "other", role)
    p = policy(two=True, version=2).model_copy(update={"record_mode": "internal_record"})
    await store.configure_expenses([p, p.model_copy(update={"legal_entity_id": "other"})])
    two = await store.get_internal_records_readiness(who, "other")
    assert two["status"] == "not_verified"
    assert one["configuration_digest"] != two["configuration_digest"]
    await store.pool.execute(
        "UPDATE finance_expense_policy SET enabled=false,generation=generation+1 WHERE legal_entity_id='e'"
    )
    assert (
        item(await store.get_internal_records_readiness(who, "e"), "expense_policy")["status"]
        == "blocked"
    )

    async def broken(conn: Any, tenant: str, entity: str) -> dict[str, Any]:
        """通过真实数据库执行无效查询，模拟存储层查询故障。"""
        await conn.fetch("SELECT * FROM finance_missing_table")
        raise AssertionError("查询应先失败")

    monkeypatch.setattr(store, "_readiness_snapshot", broken)
    with pytest.raises(Exception, match="finance_missing_table"):
        await store.get_internal_records_readiness(who, "e")


@pytest.mark.parametrize("bad_value", [None, ["private-value"], "private-value", 42, True])
async def test_nonobject_policy_revision_is_blocked_and_digest_binds_raw_value(
    store: FinanceStore,
    bad_value: Any,
) -> None:
    """合法JSONB非对象属于失效制度，保留真实坏值参与摘要且不外泄。"""
    who = await ready(store)
    first = await store.get_internal_records_readiness(who, "e")
    await store.pool.execute("ALTER TABLE finance_expense_policy_revision DISABLE TRIGGER ALL")
    await store.pool.execute(
        "UPDATE finance_expense_policy_revision SET data=$1::jsonb", json.dumps(bad_value)
    )
    report = await store.get_internal_records_readiness(who, "e")
    assert report["status"] == item(report, "expense_policy")["status"] == "blocked"
    assert report["configuration_digest"] != first["configuration_digest"]
    assert "private-value" not in json.dumps(report)
    again = await store.get_internal_records_readiness(who, "e")
    assert again["configuration_digest"] == report["configuration_digest"]
    await store.pool.execute(
        "UPDATE finance_expense_policy_revision SET data=$1::jsonb", json.dumps([bad_value])
    )
    changed = await store.get_internal_records_readiness(who, "e")
    assert changed["configuration_digest"] != report["configuration_digest"]
