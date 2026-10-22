"""F7发布包必须声明普通报销历史与仅人工的原件接口。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

from .test_package import PACKAGE, contracts, operations, validate_schema


def test_internal_records_tools_and_declared_text_contract() -> None:
    """申报原文不能附带验真状态；原件不应出现在模型工具列表。"""
    document = contracts()['finance']
    methods = operations(document)
    assert methods['listExpenseClaimHistory'][:2] == ('/v1/finance/expense-claims/{id}/history', 'get')
    assert methods['getExpenseMaterial'][:2] == ('/v1/finance/expense-claims/{id}/materials/{receipt_id}', 'get')
    assert document['info']['version'] == '0.9.0-f8'
    assert document['components']['schemas']['ExpenseDraft']['properties']['invoice_information']['maxLength'] == 20000
    validate_schema('ExpenseClaimHistoryPage', {'items': [], 'total': 0, 'next_cursor': None})
    for path in (PACKAGE / 'skills').glob('*/SKILL.md'):
        frontmatter = yaml.safe_load(path.read_text().split('---', 2)[1])
        assert 'api__finance__getExpenseMaterial' not in frontmatter.get('tool_names', [])
    expense = (PACKAGE/'skills/expense-reimbursement/SKILL.md').read_text()
    assert 'api__finance__listExpenseClaimHistory' in expense
    assert 'invoice_information' in expense and 'not_checked' in expense


async def test_real_history_and_material_match_published_schema(store: Any, tmp_path: Path) -> None:
    """真实存储、签名HTTP及原件响应均由发布schema验证。"""
    import httpx
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from .f3_contract_checks import PublishedResponses
    from .test_f0_e2e import SignedClient, app_for
    from .test_f7_records import original_doc
    from .test_worker import Gateway

    _, _, doc, objects, raw, receipt = await original_doc(store,tmp_path)
    key = Ed25519PrivateKey.generate()
    app = app_for(store,objects,Gateway({}),key)
    checks = PublishedResponses()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='https://finance') as client:
        signed = SignedClient(client,key)
        for suffix in ['/history','/materials/'+receipt]:
            path = '/v1/finance/expense-claims/'+doc['id']+suffix
            response = await signed.call('GET',path,tenant='t',user='owner',origin='human')
            assert response.status_code==200,response.text
            checks.validate('GET',path,200,response.json())
    assert raw and doc['record_mode']=='internal_record'
