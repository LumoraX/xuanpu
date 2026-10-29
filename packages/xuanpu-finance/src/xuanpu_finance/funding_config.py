"""专用资金运行配置与当前组织、基础制度交叉校验。"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_finance.config import FinanceConfig


def validate_funding_config(config: FinanceConfig) -> None:
    """资金制度必须属于明确公司和启用基础制度，审批岗位逐场景检查。"""
    companies = {(c.tenant_id,c.legal_entity_id) for c in config.companies}
    base = {(p.tenant_id,p.legal_entity_id):p for p in config.expense_policies}
    grants = {(g.tenant_id,g.legal_entity_id,g.user_id):g.role for g in config.grants}
    scopes = set()
    for policy in config.funding_policies:
        scope = (policy.tenant_id,policy.legal_entity_id)
        if scope in scopes or scope not in companies:
            raise ValueError('资金制度公司不存在或重复')
        scopes.add(scope)
        if not policy.enabled:
            continue
        if scope not in base or not base[scope].enabled or base[scope].record_mode!='invoice_backed':
            raise ValueError('启用资金制度须有当前发票基础制度')
        for chain in policy.steps.values():
            if any(grants.get((*scope,s.approver_user_id)) not in {'approver','reviewer'} for s in chain):
                raise ValueError('资金审批链须有当前同公司审批岗位')
