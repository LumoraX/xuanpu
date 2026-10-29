"""只读准备检查的已发布包必须与实际独立库结果一致。"""
from __future__ import annotations

from typing import Any

from .test_f5_domain import setup_funding
from .test_package import contracts, operations, validate_schema


async def test_current_readiness_response_matches_published_contract(store: Any) -> None:
    """真实当前公司快照验证严格schema，不能手写假passed替代。"""
    _,payer,_,_=await setup_funding(store)
    report=await store.get_finance_readiness(payer,'e')
    validate_schema('FinanceReadinessReport',report)
    assert report['production_status']=='not_verified'
    method=operations(contracts()['finance'])['getFinanceReadiness']
    assert method[:2]==('/v1/finance/readiness','get')
    assert method[2]['x-delivery-phase']=='F6'
