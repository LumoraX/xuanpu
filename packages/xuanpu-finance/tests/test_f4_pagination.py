"""真实数据库分页必须在授权过滤后有界读取，计数保持全量。"""
from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

from .test_f3_domain import configured
from .test_f3_guards import usage_plan, verified


class CountedConnection:
    """记录数据库实际返回候选集大小，不伪造查询结果。"""

    def __init__(self, connection: Any, reads: list[tuple[str, int]]) -> None:
        """保留真实连接与跨事务计数器。"""
        self.connection = connection
        self.reads = reads

    def __getattr__(self, name: str) -> Any:
        """其余行为透传真实连接。"""
        return getattr(self.connection, name)

    async def fetch(self, query: str, *args: Any) -> Any:
        """记录候选和JSON载入查询的实际返回行数。"""
        result = await self.connection.fetch(query, *args)
        self.reads.append((query, len(result)))
        return result


class CountedAcquire:
    """以原池获取真实连接后增加只读观测。"""

    def __init__(self, pool: Any, reads: list[tuple[str, int]]) -> None:
        """延迟连接获取到上下文开始时。"""
        self.context = pool.acquire()
        self.reads = reads

    async def __aenter__(self) -> CountedConnection:
        """不改变事务和服务器执行。"""
        return CountedConnection(await self.context.__aenter__(), self.reads)

    async def __aexit__(self, *args: Any) -> Any:
        """归还原连接。"""
        return await self.context.__aexit__(*args)


class CountedPool:
    """给受测列表注入透明连接观测。"""

    def __init__(self, pool: Any) -> None:
        """保存原池，供测试结束恢复。"""
        self.pool = pool
        self.reads: list[tuple[str, int]] = []

    def acquire(self) -> CountedAcquire:
        """仅观测获取后的查询。"""
        return CountedAcquire(self.pool, self.reads)


def bounded(reads: list[tuple[str, int]], limit: int) -> None:
    """候选查询包含真实LIMIT，服务器返回行数受页大小约束。"""
    assert reads
    assert all(size <= limit + 1 for _, size in reads), reads
    assert any("LIMIT" in sql.upper() for sql, _ in reads), reads


async def test_f3_sql_page_fetches_limit_plus_one_and_total_is_independent(store: Any) -> None:
    """大量不可见行也不应越过数据库边界进入Python。"""
    owner, reviewer, _ = await configured(store)
    await store.pool.execute("INSERT INTO finance_tax_confirmation(tenant_id,legal_entity_id,id,owner_id,invoice_id,operation_id,data) SELECT 't','e','fact-'||n,CASE WHEN n%2=0 THEN 'owner' ELSE 'other' END,'blue','op','{}'::jsonb FROM generate_series(1,40) n")
    original = store.pool
    counted = CountedPool(original)
    store.pool = counted
    try:
        first = await store.list_tax_confirmations(owner, "e", limit=3)
        assert first["total"] == 20 and len(first["items"]) == 3
        second = await store.list_tax_confirmations(owner, "e", first["next_cursor"], 3)
        assert second["total"] == 20
        assert not {r["id"] for r in first["items"]} & {r["id"] for r in second["items"]}
        bounded(counted.reads, 3)
        with pytest.raises(FinanceError):
            await store.list_tax_confirmations(reviewer, "e", first["next_cursor"], 3)
        with pytest.raises(FinanceError):
            await store.list_tax_confirmations(owner, None, first["next_cursor"], 3)
    finally:
        store.pool = original


async def test_f3_plans_and_review_queue_are_bounded_and_epoch_safe(store: Any) -> None:
    """统一入口按当前审批代数分页，恢复岗位不能复活旧指派。"""
    _, actor, auditor = await verified(store)
    for n in range(9):
        await usage_plan(store, actor) if n == 0 else await store.pool.execute("INSERT INTO finance_compliance_plan SELECT tenant_id,legal_entity_id,'clone-'||$1,actor_id,owner_id,target_type,target_id,target_version,digest,expires_at,data,created_at FROM finance_compliance_plan WHERE data->>'action'='confirm_usage' LIMIT 1", str(n))
    original = store.pool
    counted = CountedPool(original)
    store.pool = counted
    try:
        plans = await store.list_action_plans(actor, "e", limit=2)
        assert plans["total"] == 10
        bounded(counted.reads, 2)
    finally:
        store.pool = original
    # 由真实计划复制工作流和步骤，保持每个待办的有效代数。
    await store.pool.execute("INSERT INTO finance_compliance_workflow SELECT tenant_id,legal_entity_id,'workflow-'||n,id,1,'under_review' FROM finance_compliance_plan CROSS JOIN LATERAL (SELECT id AS n) x WHERE id LIKE 'clone-%'")
    await store.pool.execute("INSERT INTO finance_compliance_workflow_step SELECT w.tenant_id,w.legal_entity_id,'step-'||w.id,w.id,0,'audit',e.generation,'pending',1 FROM finance_compliance_workflow w JOIN finance_grant_epoch e ON(e.tenant_id=w.tenant_id AND e.legal_entity_id=w.legal_entity_id AND e.user_id='audit') WHERE w.id LIKE 'workflow-%'")
    counted.reads.clear()
    store.pool = counted
    try:
        queue = await store.list_review_queue(auditor, "e", limit=2)
        assert queue["total"] == 9
        bounded(counted.reads, 2)
    finally:
        store.pool = original
    await store.grant("t", "audit", "e", "collector")
    await store.grant("t", "audit", "e", "approver")
    assert (await store.list_action_plans(auditor, "e"))["total"] == 0
    assert (await store.list_review_queue(auditor, "e"))["total"] == 0
    assert (await store.list_compliance_plans(FinancePrincipal("t", "audit")))["total"] == 0


async def seed_visibility_records(store: Any) -> None:
    """为每类领域记录构造本人、父目标拥有者和无关来源。"""
    from .test_f3_domain import invoice

    await invoice(store, "other")
    await store.pool.execute("UPDATE finance_invoice SET owner_id='outsider' WHERE id='other'")
    for n, owner, target in [(1, "other", "blue"), (2, "owner", "other"), (3, "other", "other")]:
        args = (str(n), owner, target)
        await store.pool.execute("INSERT INTO finance_invoice_verification(tenant_id,legal_entity_id,id,owner_id,invoice_id,receipt_id,authority_revision,data,applied) VALUES('t','e',$1,$2,$3,'receipt',1,'{}',true)", *args)
        await store.pool.execute("INSERT INTO finance_tax_confirmation(tenant_id,legal_entity_id,id,owner_id,invoice_id,operation_id,data) VALUES('t','e',$1,$2,$3,'op','{}')", *args)
        await store.pool.execute("INSERT INTO finance_red_letter_case(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_case_id,authority_revision,status,data) VALUES('t','e',$1,$2,$3,$1,1,'issued','{}')", *args)
        await store.pool.execute("INSERT INTO finance_tax_adjustment(tenant_id,legal_entity_id,id,owner_id,invoice_id,external_adjustment_id,status,data) VALUES('t','e',$1,$2,$3,$1,'required','{}')", *args)
        await store.pool.execute("INSERT INTO finance_archive(tenant_id,legal_entity_id,id,owner_id,target_type,target_id,operation_id,manifest_hash,source_snapshot_hash,snapshot,data) VALUES('t','e',$1,$2,'invoice',$3,$1,$1,'hash','{}','{}')", *args)
        await store.pool.execute("INSERT INTO finance_archive_audit(tenant_id,legal_entity_id,id,owner_id,archive_id,operation_id,data) VALUES('t','e',$1,'other',$1,$1,'{}')", str(n))
        await store.pool.execute("INSERT INTO finance_archive_supplement(tenant_id,legal_entity_id,id,owner_id,archive_id,child_archive_id,source_event_id) VALUES('t','e',$1,'other',$1,$1,$1)", str(n))


async def assert_visibility_equivalent(store: Any) -> None:
    """SQL逐类对照保留的单记录权限实现，不以查询自证权限。"""
    from xuanpu_finance.storage.f3_queries import TABLES
    from xuanpu_finance.storage.f4_visibility import visibility

    async with store.pool.acquire() as conn:
        for user in ("owner", "tax", "audit", "other", "absent"):
            who = FinancePrincipal("t", user)
            for name, table in TABLES.items():
                rows = await conn.fetch(f"SELECT * FROM {table}")
                expected = {r["id"] for r in rows if await store._f3_visible(conn, who, r, name)}
                actual = await conn.fetch(f"SELECT r.id FROM {table} r WHERE r.tenant_id=$1 AND {visibility(name, user='$2')}", "t", user)
                assert {r["id"] for r in actual} == expected, (name, user)


async def test_sql_visibility_matches_single_record_for_every_f3_kind(store: Any) -> None:
    """本人、审核、指派、父目标及递归档案在撤权恢复后仍逐项等价。"""
    _, actor, auditor = await verified(store)
    await usage_plan(store, actor)
    await store.grant("t", "other", "e", "collector")
    await seed_visibility_records(store)
    await assert_visibility_equivalent(store)
    await store.grant("t", auditor.user_id, "e", "collector")
    await assert_visibility_equivalent(store)
    await store.grant("t", auditor.user_id, "e", "approver")
    await assert_visibility_equivalent(store)
    # 渠道和制度代数变化也撤销通过父目标获取证据的权限。
    await store.pool.execute("UPDATE finance_compliance_policy SET generation=generation+1")
    await assert_visibility_equivalent(store)


async def test_company_none_parent_filter_and_cursor_scope(store: Any) -> None:
    """跨公司列表只统计实际授权公司，父过滤进入计数与页查询。"""
    owner, _, _ = await configured(store)
    await store.seed_company("t", "elsewhere", "其他公司", "OTHER")
    await store.pool.execute("INSERT INTO finance_tax_confirmation(tenant_id,legal_entity_id,id,owner_id,invoice_id,operation_id,data) SELECT 't',CASE WHEN n<6 THEN 'e' ELSE 'elsewhere' END,'fact-'||n,'owner','invoice','op','{}'::jsonb FROM generate_series(1,10) n")
    result = await store.list_tax_confirmations(owner, limit=2)
    assert result["total"] == 5
    await store.grant("t", "owner", "elsewhere", "collector")
    assert (await store.list_tax_confirmations(owner, limit=2))["total"] == 10
    await store.pool.execute("INSERT INTO finance_archive_audit(tenant_id,legal_entity_id,id,owner_id,archive_id,operation_id,data) SELECT 't','e','audit-'||n,'owner',CASE WHEN n<5 THEN 'parent' ELSE 'different' END,'op','{}'::jsonb FROM generate_series(1,10) n")
    first = await store._f3_list(owner, "audits", "e", None, 2, parent_id="parent")
    assert first["total"] == 4 and len(first["items"]) == 2
    second = await store._f3_list(owner, "audits", "e", first["next_cursor"], 2, parent_id="parent")
    assert second["total"] == 4 and second["next_cursor"] is None
    with pytest.raises(FinanceError):
        await store._f3_list(owner, "audits", "e", first["next_cursor"], 2, parent_id="different")


async def test_mixed_f2_f3_plans_and_queue_keep_stable_pages_without_duplicates(store: Any) -> None:
    """两个真实审批领域合并后依然独立计数、绑定过滤及稳定续读。"""
    from xuanpu_finance.expenses.models import ActionPreview

    from .test_f2_domain import approved_payable, ref

    _, payer, _, approver, doc = await approved_payable(store)
    external = await store.preview_finance_action(payer, ActionPreview(action="execute_payment", target_type="payable", target_id=doc["id"], expected_version=doc["version"], parameters={"amount": "100.00", "payee_ref": ref("supplier_account", "account")}), "mixed-payment")
    _, actor, _ = await verified(store)
    compliance = await usage_plan(store, actor)
    await store.pool.execute("UPDATE finance_compliance_workflow_step SET approver_user_id='a',authority_generation=(SELECT generation FROM finance_grant_epoch WHERE tenant_id='t' AND legal_entity_id='e' AND user_id='a')")
    first = await store.list_action_plans(approver, "e", limit=1)
    second = await store.list_action_plans(approver, "e", cursor=first["next_cursor"], limit=1)
    assert first["total"] == second["total"] == 2
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {external["id"], compliance["id"]}
    assert second["next_cursor"] is None
    with pytest.raises(FinanceError):
        await store.list_action_plans(approver, "e", target_id="blue", cursor=first["next_cursor"], limit=1)
    target = await store.list_action_plans(approver, "e", target_id=doc["id"], limit=1)
    assert target["total"] == 1 and target["items"][0]["id"] == external["id"]
    original = store.pool
    counted = CountedPool(original)
    store.pool = counted
    try:
        queue = await store.list_review_queue(approver, "e", limit=1)
        next_queue = await store.list_review_queue(approver, "e", cursor=queue["next_cursor"], limit=1)
        assert queue["total"] == next_queue["total"] == 2
        assert {queue["items"][0]["target_id"], next_queue["items"][0]["target_id"]} == {external["id"], compliance["id"]}
        # 仅约束候选查询；当前页单记录的证据集合独立于候选集大小。
        candidates = [(sql, size) for sql, size in counted.reads if "UNION ALL" in sql]
        bounded(candidates, 1)
        assert all("data" not in sql.split("FROM finance_action_plan")[0] for sql, _ in candidates)
    finally:
        store.pool = original
    await store.grant("t", "a", "e", "collector")
    await store.grant("t", "a", "e", "approver")
    assert (await store.list_review_queue(approver, "e"))["total"] == 0
    assert (await store.list_action_plans(approver, "e"))["total"] == 0


class ConcurrentInsertConnection(CountedConnection):
    """在真实count完成之后从另一连接提交新事实，验证同快照页面。"""

    def __init__(self, connection: Any, pool: Any) -> None:
        """保留独立连接的池。"""
        super().__init__(connection, [])
        self.pool = pool

    async def fetchval(self, query: str, *args: Any) -> Any:
        """计数建立快照后提交第二个同授权事实。"""
        result = await self.connection.fetchval(query, *args)
        if query.startswith("SELECT count(*) FROM finance_tax_confirmation"):
            await self.pool.execute("INSERT INTO finance_tax_confirmation(tenant_id,legal_entity_id,id,owner_id,invoice_id,operation_id,data) VALUES('t','e','concurrent','owner','invoice','op','{}')")
        return result


async def test_count_and_page_share_repeatable_read_snapshot(store: Any) -> None:
    """并发提交不让独立count和候选页混入不同事务快照。"""
    owner, _, _ = await configured(store)
    await store.pool.execute("INSERT INTO finance_tax_confirmation(tenant_id,legal_entity_id,id,owner_id,invoice_id,operation_id,data) VALUES('t','e','before','owner','invoice','op','{}')")
    original = store.pool

    class SnapshotAcquire(CountedAcquire):
        """沿用真实获取和释放，在连接上注入一次并发提交。"""

        async def __aenter__(self) -> ConcurrentInsertConnection:
            """只包装当前受测查询连接。"""
            return ConcurrentInsertConnection(await self.context.__aenter__(), original)

    class SnapshotPool(CountedPool):
        """仅当前列表使用可控并发连接。"""

        def acquire(self) -> SnapshotAcquire:
            """数据库执行顺序保持count、独立插入、候选页。"""
            return SnapshotAcquire(self.pool, self.reads)

    store.pool = SnapshotPool(original)
    try:
        result = await store.list_tax_confirmations(owner, "e", limit=5)
        assert result["total"] == 1
        assert [r["id"] for r in result["items"]] == ["before"]
    finally:
        store.pool = original
    assert (await store.list_tax_confirmations(owner, "e"))["total"] == 2


@pytest.mark.parametrize("parent", ["invoice", "red_letter_case", "expense_claim"])
async def test_parent_target_assignment_and_red_invoice_visibility_are_equivalent(store: Any, parent: str) -> None:
    """发票、红字父单及费用分配和红票链接均保持原必要证据读取语义。"""
    from .test_expense_domain import allocate, claim, setup

    owner, actor, auditor = await verified(store)
    plan = await usage_plan(store, actor)
    await setup(store)
    document = await claim(store, owner, "20.00")
    await allocate(store, owner, document, id="blue", amount="20.00")
    await seed_visibility_records(store)
    await store.pool.execute("INSERT INTO finance_red_letter_link(tenant_id,legal_entity_id,id,case_id,blue_invoice_id,red_invoice_id,actor_id,amount,tax,data) VALUES('t','e','link','1','blue','other','tax',10,1,'{}')")
    target = {"invoice": "blue", "red_letter_case": "1", "expense_claim": document["id"]}[parent]
    # 不可变旧计划保留；新工作流重新指向等价的受控目标索引，隔离各父目标分支。
    await store.pool.execute("INSERT INTO finance_compliance_plan SELECT tenant_id,legal_entity_id,'parent-plan',actor_id,owner_id,$2,$3,target_version,digest,expires_at,data,created_at FROM finance_compliance_plan WHERE id=$1", plan["id"], parent, target)
    await store.pool.execute("UPDATE finance_compliance_workflow SET plan_id='parent-plan'")
    await assert_visibility_equivalent(store)
    async with store.pool.acquire() as conn:
        assert await store._f3_assigned(conn, auditor, "e", "invoice", "blue")
        assert await store._f3_assigned(conn, auditor, "e", "invoice", "other") is (parent != "red_letter_case")
    if parent == "expense_claim":
        await store.pool.execute("INSERT INTO finance_archive(tenant_id,legal_entity_id,id,owner_id,target_type,target_id,operation_id,manifest_hash,source_snapshot_hash,snapshot,data) VALUES('t','e','expense-archive','other','expense_claim',$1,'expense-op','expense-manifest','hash','{}','{}')", document["id"])
        await assert_visibility_equivalent(store)
    await store.pool.execute("UPDATE finance_compliance_provider SET generation=generation+1")
    await assert_visibility_equivalent(store)
