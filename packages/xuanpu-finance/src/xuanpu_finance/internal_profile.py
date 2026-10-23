"""内部记录配置、HTTP白名单与共用计划的显式边界。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from xuanpu_finance.errors import FinanceError

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

    from xuanpu_finance.config import FinanceConfig
    from xuanpu_finance.models import FinancePrincipal

PROFILES = frozenset({'general', 'internal_records'})
INTERNAL_ACTIONS = frozenset({'submit_claim', 'approve', 'reject', 'withdraw'})
INTERNAL_OPERATIONS = frozenset({
    'getFinanceCapabilities', 'listLegalEntities', 'listExpenseCategories', 'listCostCenters',
    'importSupportingMaterials', 'listSupportingMaterialImports', 'getSupportingMaterialImport',
    'getInternalRecordsReadiness', 'getOperation', 'cancelOperation', 'createExpenseClaim', 'listExpenseClaims',
    'listExpenseClaimSummaries', 'getExpenseClaim', 'updateExpenseClaim',
    'listExpenseClaimHistory', 'getExpenseMaterial', 'listReviewQueue',
    'previewFinanceAction', 'getActionPlan', 'executeFinanceAction',
    'listFinanceNotifications', 'readFinanceNotification', 'getFinanceOverview',
})


def validate_internal_config(config: FinanceConfig) -> None:
    """内部实例必须有真实待填后的公司制度和独立collector，不能带外部配置。"""
    if config.profile != 'internal_records':
        return
    if config.synthetic or config.execution_simulation or not config.companies:
        raise ValueError('内部记录必须有公司且禁止合成或模拟')
    external = ('funding_policies', 'settlement_policies', 'compliance_policies',
                'settlement_providers', 'compliance_providers', 'business_documents', 'payee_accounts')
    if any(getattr(config, name) for name in external):
        raise ValueError('内部记录不能配置外部业务制度或目录')
    if has_placeholder(config.model_dump(mode='json')):
        raise ValueError('内部记录配置不能保留REPLACE_占位标识')
    hostname = urlsplit(config.gateway.base_url).hostname or ''
    if hostname == 'invalid' or hostname.endswith('.invalid'):
        raise ValueError('内部记录不能使用.invalid网关')
    policies = {(p.tenant_id, p.legal_entity_id): p for p in config.expense_policies}
    for company in config.companies:
        scope = (company.tenant_id, company.legal_entity_id)
        policy = policies.get(scope)
        if policy is None or not policy.enabled or policy.record_mode != 'internal_record':
            raise ValueError('每公司必须启用internal_record费用制度')
        approvers = {step.approver_user_id for step in policy.steps}
        collectors = {g.user_id for g in config.grants
                      if (g.tenant_id, g.legal_entity_id) == scope and g.role == 'collector'}
        if not collectors - approvers:
            raise ValueError('每公司至少需要不在审批链的collector')


def has_placeholder(value: Any) -> bool:
    """递归检查固定配置中的模板标识，不读取任何秘密文件。"""
    if isinstance(value, str):
        return 'REPLACE_' in value.upper()
    if isinstance(value, dict):
        return any(has_placeholder(k) or has_placeholder(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_placeholder(v) for v in value)
    return False


def guard_internal_plan(request: Request, target_type: str, action: str) -> None:
    """不可变计划和预检参数分别验证，不能借共用入口隧穿其他财务能力。"""
    if request.app.state.profile == 'internal_records' and (
        target_type != 'expense_claim' or action not in INTERNAL_ACTIONS
    ):
        raise FinanceError('INTERNAL_RECORDS_ONLY', 403, '当前实例仅允许内部报销记录')


async def guard_internal_target(
    request: Request, who: FinancePrincipal, target_type: str, target_id: str, action: str,
) -> None:
    """计划metadata之外还核对实际单据类型，错误声明不能留下通用目标计划。"""
    guard_internal_plan(request, target_type, action)
    if request.app.state.profile == 'internal_records':
        await request.app.state.store.get_expense_claim(who, target_id)


def include_finance_routers(app: FastAPI, routers: list[Any]) -> None:
    """内部实例只注册白名单路由，未列出的业务和回调在路由层不存在。"""
    from fastapi import APIRouter

    for router in routers:
        if app.state.profile == 'general':
            app.include_router(router)
            continue
        selected = APIRouter()
        selected.routes.extend(r for r in router.routes
                               if getattr(r, 'operation_id', None) in INTERNAL_OPERATIONS)
        app.include_router(selected)
