"""F5实际存储响应及材料回执必须符合发布的严格契约。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import jsonschema
import pytest

from .test_f5_archive import real_funding_source
from .test_f5_domain import approve, draft, setup_funding, submit
from .test_package import validate_schema

if TYPE_CHECKING:
    from pathlib import Path


async def test_funding_results_match_published_schema(store: Any, tmp_path: Path) -> None:
    """验证实际草稿、分页、选项、批准及固定行动上下文而非手写响应。"""
    from .test_f5_http_e2e import enable_provider, external_plan
    owner,payer,_,approver=await setup_funding(store)
    doc=await store.create_funding_case(owner,draft(),'case')
    validate_schema('BusinessDocument',doc)
    validate_schema('FundingCasePage',await store.list_funding_cases(owner,'e'))
    validate_schema('FundingOptions',await store.get_funding_options(owner,'e'))
    doc=await approve(store,owner,approver,await submit(store,owner,doc))
    await enable_provider(store)
    from .test_f2_domain import ref
    plan=await external_plan(store,payer,approver,doc,'execute_payment',payee_ref=ref('supplier_account','account'))
    validate_schema('ActionPlan',plan)
    validate_schema('FundingAuthority',doc['funding']['authority'])


async def test_supporting_material_operation_schema(store: Any,tmp_path: Path) -> None:
    """真实无票PDF收件回执含purpose/materials，普通发票响应保持原字段。"""
    owner,_,_,_,_,_=await real_funding_source(store,tmp_path)
    id=await store.pool.fetchval("SELECT id FROM finance_operation WHERE tenant_id='t' AND legal_entity_id='e' AND purpose='supporting_material'")
    validate_schema('Operation',await store.get_operation(owner,id))


@pytest.mark.parametrize('scenario',['prepayment','employee_advance','entrusted_collection','prepayment_clearance','prepayment_return'])
def test_funding_command_matrix_does_not_accept_forged_result(scenario: str) -> None:
    """领域和公开草稿均拒绝结果/身份字段，场景原凭据限制不被宽泛schema放开。"""
    body=draft(scenario,**({'source_case_id':'parent'} if scenario in {'prepayment_clearance','prepayment_return'} else {})).model_dump(mode='json')
    validate_schema('FundingDraft',body)
    body['verified']=True
    with pytest.raises(jsonschema.ValidationError):
        validate_schema('FundingDraft',body)
