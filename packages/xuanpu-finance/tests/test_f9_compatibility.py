"""内部实例启动前只读持久库兼容门禁，覆盖全部租户。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from xuanpu_finance import runtime
from xuanpu_finance.config import FinanceConfig
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

from .test_expense_domain import claim, invoice
from .test_f7_records import configured
from .test_f9_profile import internal_settings
from .test_worker import ref

if TYPE_CHECKING:
    from xuanpu_finance.storage import FinanceStore


async def check(store: FinanceStore) -> None:
    """使用真实连接池运行公开的迁移前检查。"""
    function=getattr(runtime,'check_internal_database',None)
    assert function is not None, '内部只读兼容门禁尚未实现'
    await function(store.pool)


async def test_empty_and_internal_database_allow_readonly_check(store: FinanceStore) -> None:
    """已迁移空库以及普通内部记录可检查且不改变持久状态。"""
    await check(store)
    owner,_=await configured(store)
    await claim(store,owner)
    before=await store.pool.fetchval('SELECT count(*) FROM finance_expense_claim')
    await check(store)
    assert await store.pool.fetchval('SELECT count(*) FROM finance_expense_claim')==before


@pytest.mark.parametrize('state',['accepted','running'])
async def test_pending_invoice_import_rejected_before_migrate(store: FinanceStore,state: str) -> None:
    """尚未生成发票的导入也阻断，configure_store不能先改写公司/制度。"""
    owner,_=await configured(store)
    op=await store.accept_import(owner,'e',[ref('pending',b'%PDF-test')],'pending')
    await store.pool.execute('UPDATE finance_operation SET status=$2 WHERE id=$1',op['id'],state)
    before=await store.pool.fetch('SELECT * FROM finance_grant ORDER BY user_id')
    config=FinanceConfig.model_validate(internal_settings())
    with pytest.raises(FinanceError,match='INTERNAL_DATABASE_INCOMPATIBLE'):
        await runtime.configure_store(store.pool,config)
    assert await store.pool.fetch('SELECT * FROM finance_grant ORDER BY user_id')==before
    assert await store.pool.fetchval('SELECT purpose FROM finance_operation WHERE id=$1',op['id'])=='invoice'


@pytest.mark.parametrize('kind',['invoice','noninternal_claim','simulation','unknown_old_column','foreign_tenant'])
async def test_general_history_and_unknown_schema_fail_closed(store: FinanceStore,kind: str) -> None:
    """跨租户业务、模拟标记与无法确认的旧结构都在写入前拒绝。"""
    owner,_=await configured(store)
    if kind=='invoice':
        await invoice(store)
    elif kind=='noninternal_claim':
        doc=await claim(store,owner)
        await store.pool.execute("UPDATE finance_expense_claim SET record_mode='invoice_backed' WHERE id=$1",doc['id'])
    elif kind=='simulation':
        await store.pool.execute('INSERT INTO finance_settlement_mode VALUES(true,true)')
    elif kind=='unknown_old_column':
        await store.pool.execute('ALTER TABLE finance_operation DROP COLUMN purpose')
    else:
        await store.seed_company('other','e','其他租户','OTHER')
        await store.grant('other','person','e')
        await store.accept_import(FinancePrincipal('other','person','human'),'e',[ref('foreign',b'%PDF')],'foreign')
    with pytest.raises(FinanceError,match='INTERNAL_DATABASE_INCOMPATIBLE'):
        await check(store)


@pytest.mark.parametrize('table,column',[('finance_invoice','source_simulation'),('finance_payment','simulation'),('finance_compliance_operation','simulation')])
async def test_missing_simulation_columns_fail_closed_even_empty(store: FinanceStore,table: str,column: str) -> None:
    """空业务表缺少模拟来源列也属于未知旧结构，不能以空查询放行。"""
    await store.pool.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    with pytest.raises(FinanceError,match='INTERNAL_DATABASE_INCOMPATIBLE'):
        await check(store)


@pytest.mark.parametrize('change',[
    'ALTER TABLE finance_item DROP COLUMN content_type',
    'ALTER TABLE finance_receipt DROP COLUMN sha256',
    'DROP TABLE finance_expense_history',
    'ALTER TABLE finance_item ALTER COLUMN content_type TYPE varchar(32)',
])
async def test_missing_core_structure_rejected_before_all_writes(store: FinanceStore,change: str,monkeypatch: pytest.MonkeyPatch) -> None:
    """F8支持结构中任何必要表列缺失或类型改变，均在迁移和授权写入前拒绝。"""
    await store.pool.execute(change)
    async def forbidden(*args: object,**kwargs: object) -> None:
        """结构未知时迁移与配置不可触发，防止先修补或部分写入。"""
        raise AssertionError('unknown schema reached a write boundary')
    monkeypatch.setattr(type(store),'migrate',forbidden)
    monkeypatch.setattr(type(store),'configure',forbidden)
    before=await store.pool.fetchval('SELECT count(*) FROM finance_grant')
    with pytest.raises(FinanceError,match='INTERNAL_DATABASE_INCOMPATIBLE'):
        await runtime.configure_store(store.pool,FinanceConfig.model_validate(internal_settings()))
    assert await store.pool.fetchval('SELECT count(*) FROM finance_grant')==before
