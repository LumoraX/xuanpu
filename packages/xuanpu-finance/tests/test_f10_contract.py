"""F10内部试用报告的精确脱敏投影及Skill可用边界。"""
from __future__ import annotations

from copy import deepcopy

import jsonschema
import pytest
import yaml

from .test_package import PACKAGE, contracts, operations, validate_schema


def report_value() -> dict:
    """构造不携带真实配置或许可结论的报告响应。"""
    codes = ['company', 'expense_policy', 'expense_categories', 'approval_roles',
        'collector_separation', 'grant_epochs', 'external_configuration',
        'simulation_disabled', 'runtime_mode', 'runtime_instance_lock',
        'runtime_workers', 'runtime_external_workers']
    return {'profile': 'internal_records', 'legal_entity_id': 'e',
        'generated_at': '2026-10-23T00:00:00Z', 'status': 'not_verified',
        'production_status': 'not_verified', 'configuration_digest': 'a'*64,
        'checks': [{'code': c, 'status': 'passed', 'detail': '固定核对解释'} for c in codes],
        'versions': [{'kind': 'expense', 'version': 1, 'generation': 1,
                      'enabled': True, 'digest': 'b'*64}],
        'external_verifications': ['formal_policy_approval', 'production_identity',
            'database_and_original_isolation', 'offsite_disaster_recovery',
            'real_model', 'production_load'],
        'runtime': {'mode': 'read_only', 'instance_lock_lost': False,
            'receipt_worker': 'running', 'notification_worker': 'running',
            'external_workers': 'absent'}}


def test_internal_readiness_response_has_exact_bounded_metadata() -> None:
    """正式已验、正文、额外配置和未知状态均不属于报告契约。"""
    path, method, op = operations(contracts()['finance'])['getInternalRecordsReadiness']
    assert (path, method) == ('/v1/finance/internal-records-readiness', 'get')
    assert op['x-delivery-phase'] == 'F10'
    good = report_value()
    validate_schema('InternalRecordsReadiness', good)
    for patch in [{'status': 'passed'}, {'production_status': 'verified'},
                  {'profile': 'general'}, {'content': 'private'}]:
        with pytest.raises(jsonschema.ValidationError):
            validate_schema('InternalRecordsReadiness', {**good, **patch})
    for section, patch in [('runtime', {'receipt_worker': 'unknown'}),
                           ('runtime', {'url': 'private'})]:
        bad = deepcopy(good)
        bad[section].update(patch)
        with pytest.raises(jsonschema.ValidationError):
            validate_schema('InternalRecordsReadiness', bad)
    bad = deepcopy(good)
    bad['versions'][0]['approver_user_id'] = 'private'
    with pytest.raises(jsonschema.ValidationError):
        validate_schema('InternalRecordsReadiness', bad)


def test_review_skill_can_read_internal_report_but_cannot_approve() -> None:
    """报告只读工具不构成人工执行、原件或通用外部准备授权。"""
    text = (PACKAGE/'skills/finance-review/SKILL.md').read_text()
    frontmatter = yaml.safe_load(text.split('---', 2)[1])
    assert 'api__finance__getInternalRecordsReadiness' in frontmatter['tool_names']
    for name in ['executeFinanceAction', 'getExpenseMaterial']:
        assert 'api__finance__'+name not in frontmatter['tool_names']
    branch = text.split('## internal_records 分支', 1)[1].split('## general 分支', 1)[0]
    assert 'getInternalRecordsReadiness' in branch and 'not_verified' in branch
