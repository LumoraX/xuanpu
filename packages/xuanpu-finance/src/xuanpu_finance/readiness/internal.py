"""内部记录专用离线报告，只核配置和固定资源，不构成生产许可。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_finance.operations import request_digest
from xuanpu_finance.readiness.models import ReadinessCheck, ReadinessReport, check

if TYPE_CHECKING:
    from xuanpu_finance.config import FinanceConfig


def internal_report(
    config: FinanceConfig, snapshots: list[dict[str, Any]], resources: list[ReadinessCheck],
) -> ReadinessReport:
    """复核可变配置并只输出固定检查与非秘密摘要，支付和官方查验不作为前提。"""
    try:
        type(config).model_validate(config.model_dump())
        valid = True
    except (ValidationError, ValueError):
        valid = False
    checks = [*resources, check('internal_records_configuration', valid,
                               '公司、制度、岗位及内部记录边界须完整配置')]
    versions = [{'kind': 'expense', 'version': p.version, 'enabled': p.enabled}
                for p in config.expense_policies]
    return {
        'status': 'blocked' if any(c['status'] == 'blocked' for c in checks) else 'not_verified',
        'production_status': 'not_verified', 'checks': checks, 'versions': versions,
        'configuration_digest': request_digest({
            'profile': config.profile, 'mode': config.mode, 'environment': config.environment,
            'issuer': config.issuer, 'audience': config.audience, 'snapshots': snapshots}),
        'external_verifications': ['formal_policy_approval', 'production_identity',
                                   'database_and_original_storage', 'offsite_disaster_recovery',
                                   'real_model', 'production_load'],
    }
