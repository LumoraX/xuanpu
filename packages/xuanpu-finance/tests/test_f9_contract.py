"""F9摘要与实例能力的已发布契约约束。"""
from __future__ import annotations

from copy import deepcopy

import jsonschema
import pytest
import yaml

from .test_package import PACKAGE, contracts, operations, validate_schema


def test_summary_contract_has_bounded_exact_projection() -> None:
    """摘要不带申报原文，分页容量与最长事由均为明确契约。"""
    document = contracts()['finance']
    index = operations(document)
    path, method, operation = index['listExpenseClaimSummaries']
    assert (path, method) == ('/v1/finance/expense-claim-summaries', 'get')
    assert operation['x-delivery-phase'] == 'F9'
    schemas = document['components']['schemas']
    fields = {'id', 'legal_entity_id', 'owner_id', 'version', 'status', 'total',
              'reason', 'created_at', 'record_mode'}
    assert set(schemas['ExpenseClaimSummary']['properties']) == fields
    assert set(schemas['ExpenseClaimSummary']['required']) == fields
    assert schemas['ExpenseClaimSummary']['additionalProperties'] is False
    assert schemas['ExpenseClaimSummaryPage']['properties']['items']['maxItems'] == 50
    params = {item['name']: item for item in operation['parameters'] if 'name' in item}
    assert params['limit']['schema']['anyOf'][0] == {'type': 'integer', 'minimum': 1,
                                       'maximum': 50, 'default': 20}
    assert params['view']['schema']['anyOf'][0]['default'] == 'mine'
    assert params['q']['schema']['anyOf'][0]['maxLength'] == 128
    item = {'id': 'claim', 'legal_entity_id': 'e', 'owner_id': 'u', 'version': 1,
            'status': 'draft', 'total': '10.00', 'reason': '\x01' * 2000,
            'created_at': '2026-10-22T00:00:00Z', 'record_mode': 'internal_record'}
    validate_schema('ExpenseClaimSummaryPage', {'items': [item], 'total': 4,
                                              'next_cursor': None})
    bad = deepcopy(item)
    bad['content'] = {'invoice_information': 'private'}
    with pytest.raises(jsonschema.ValidationError):
        validate_schema('ExpenseClaimSummary', bad)


def test_capabilities_contract_is_exact_runtime_metadata() -> None:
    """实例能力不是个人权限、审批回执或上线验收结果。"""
    document = contracts()['finance']
    assert operations(document)['getFinanceCapabilities'][:2] == (
        '/v1/finance/capabilities', 'get')
    value = {'profile': 'internal_records', 'mode': 'read_only',
             'service_version': '2026.10.22.6', 'internal_records_only': True}
    validate_schema('FinanceCapabilities', value)
    for key, replacement in [('profile', 'unknown'), ('mode', 'automatic'),
                             ('internal_records_only', 'true')]:
        with pytest.raises(jsonschema.ValidationError):
            validate_schema('FinanceCapabilities', {**value, key: replacement})
    with pytest.raises(jsonschema.ValidationError):
        validate_schema('FinanceCapabilities', {**value, 'approved': True})


def test_skills_discover_profile_without_human_tools() -> None:
    """所有入口先读能力，报销使用摘要检索且不取得人工执行或原件工具。"""
    for path in (PACKAGE / 'skills').glob('*/SKILL.md'):
        frontmatter = yaml.safe_load(path.read_text().split('---', 2)[1])
        tools = frontmatter.get('tool_names', [])
        assert 'api__finance__getFinanceCapabilities' in tools
        assert 'api__finance__executeFinanceAction' not in tools
        assert 'api__finance__getExpenseMaterial' not in tools
    expense = (PACKAGE / 'skills/expense-reimbursement/SKILL.md').read_text()
    assert 'api__finance__listExpenseClaimSummaries' in expense
