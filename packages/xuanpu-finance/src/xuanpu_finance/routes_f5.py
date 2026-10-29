"""专用资金业务与支持材料收件的独立财务HTTP入口。"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store, require_write
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import Reason, StrictModel, Version
from xuanpu_finance.funding.models import (  # noqa: TC001
    FundingDraft,
    FundingScenario,
    FundingUpdate,
)
from xuanpu_finance.models import SupportingMaterialImport  # noqa: TC001 — FastAPI运行时解析
from xuanpu_finance.routes import Company, Cursor, IdempotencyKey, Limit  # noqa: TC001

router = APIRouter()
Scenario = Annotated[FundingScenario | None, Query()]
Status = Annotated[Literal['draft','submitted','under_review','approved','rejected','withdrawn'] | None, Query()]


@router.post('/v1/finance/supporting-material-imports',status_code=202,operation_id='importSupportingMaterials')
async def import_materials(request: Request,body: SupportingMaterialImport,key: IdempotencyKey,who: FinanceCaller) -> dict[str,Any]:
    """仅受理真实来源固定版本，是否完整保存由持久操作查询。"""
    require_write(request)
    return await get_store(request).accept_supporting_materials(who,body.legal_entity_id,body.files,key)


@router.post('/v1/finance/funding-cases',status_code=201,operation_id='createFundingCase')
async def create_case(request: Request,body: FundingDraft,key: IdempotencyKey,who: FinanceCaller) -> dict[str,Any]:
    """建立严格专用草稿，身份、状态和环境由独立服务决定。"""
    require_write(request)
    return await get_store(request).create_funding_case(who,body,key)


@router.get('/v1/finance/funding-cases',operation_id='listFundingCases')
async def list_cases(request: Request,who: FinanceCaller,legal_entity_id: Company,cursor: Cursor=None,limit: Limit=50,scenario: Scenario=None,status: Status=None) -> dict[str,Any]:
    """按当前主体和公司SQL分页，total不随当前页改变。"""
    return await get_store(request).list_funding_cases(who,legal_entity_id,cursor=cursor,limit=limit,scenario=scenario,status=status)


@router.get('/v1/finance/funding-cases/{id}',operation_id='getFundingCase')
async def get_case(request: Request,id: str,who: FinanceCaller) -> dict[str,Any]:
    """恢复原材料、实际流程和独立资金余额，不依赖聊天记忆。"""
    return await get_store(request).get_funding_case(who,id)


@router.put('/v1/finance/funding-cases/{id}',operation_id='updateFundingCase')
async def update_case(request: Request,id: str,body: FundingUpdate,key: IdempotencyKey,who: FinanceCaller) -> dict[str,Any]:
    """CAS修订可编辑草稿，不覆盖批准、付款或最终核销历史。"""
    require_write(request)
    return await get_store(request).update_funding_case(who,id,body,key)


@router.get('/v1/finance/funding-options',operation_id='getFundingOptions')
async def options(request: Request,who: FinanceCaller,legal_entity_id: Company) -> dict[str,Any]:
    """只展示已配置的内部场景及审批链，不能据此认定正式通道资格。"""
    return await get_store(request).get_funding_options(who,legal_entity_id)


class FundingRevocation(StrictModel):
    """实际人工撤销的明确理由和乐观版本，不接收身份或结果。"""
    expected_version: Version
    reason: Reason


@router.post('/v1/finance/funding-authorities/{id}/revoke',operation_id='revokeFundingAuthority')
async def revoke(request: Request,id: str,body: FundingRevocation,key: IdempotencyKey,who: FinanceCaller) -> dict[str,Any]:
    """撤销仅阻断未发送资格，不宣称撤回已经支付的资金。"""
    require_write(request,cancel=True)
    return await get_store(request).revoke_funding_authority(who,id,body.expected_version,body.reason,key)


async def funding_action_target(request: Request,who: FinanceCaller,kind: str,id: str) -> bool:
    """审批原计划需还原实际资金目标，防止排空时借取消标签新批准。"""
    for _ in range(3):
        if kind=='funding_case':
            return True
        if kind!='action_plan':
            return False
        plan=await get_store(request).get_action_plan(who,id)
        kind,id=plan['target_type'],plan['target_id']
    raise FinanceError('ACTION_TARGET_INVALID',422,'行动计划目标层级无效，请核对原计划')


@router.get('/v1/finance/funding-cases/{id}/materials/{receipt_id}',operation_id='getFundingMaterial')
async def material(request: Request,id: str,receipt_id: str,who: FinanceCaller,
                   offset: Annotated[int,Query(ge=0)]=0,
                   limit: Annotated[int,Query(ge=1,le=262144)]=131072) -> dict[str,Any]:
    """当前有权员工实际读取原材料分块，材料核对不只依赖编号或摘要。"""
    from xuanpu_finance.funding.materials import read_material
    if request.app.state.objects is None:
        raise FinanceError('MATERIAL_STORE_UNAVAILABLE',503,'独立原件存储未注入，请联系财务管理员')
    return await read_material(get_store(request),request.app.state.objects,who,id,receipt_id,offset,limit)
