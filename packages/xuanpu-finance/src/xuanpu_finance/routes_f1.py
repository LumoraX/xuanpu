"""报销、分配、持久审批与受控目录的独立财务API。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request

from xuanpu_finance.compliance.models import ComplianceActionPreview  # noqa: TC001
from xuanpu_finance.dependencies import FinanceCaller, get_store, require_write
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.expenses.models import (  # noqa: TC001 — FastAPI运行时解析
    ActionExecution,
    ActionPreview,
    AllocationRequest,
    ExceptionResolution,
    ExpenseDraft,
    ExpenseUpdate,
)
from xuanpu_finance.internal_profile import guard_internal_target
from xuanpu_finance.routes import (  # noqa: TC001 — FastAPI运行时解析
    Company,
    Cursor,
    IdempotencyKey,
    Limit,
    VersionRequest,
)
from xuanpu_finance.routes_f5 import funding_action_target

router = APIRouter()
ClaimStatus = Annotated[
    Literal["draft", "submitted", "under_review", "approved", "rejected", "withdrawn"] | None,
    Query(),
]
CenterKind = Annotated[Literal["department", "project"] | None, Query()]


@router.post("/v1/finance/expense-claims", status_code=201, operation_id="createExpenseClaim")
async def create_claim(
    request: Request, body: ExpenseDraft, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """建立本人草稿，不预留发票额度。"""
    require_write(request)
    return await get_store(request).create_expense_claim(who, body, key)


@router.get("/v1/finance/expense-claims", operation_id="listExpenseClaims")
async def list_claims(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
    status: ClaimStatus = None,
) -> dict[str, Any]:
    """按实际权限恢复草稿和审批记录，total独立于当前页。"""
    return await get_store(request).list_expense_claims(
        who, legal_entity_id, cursor=cursor, limit=limit, status=status
    )


@router.get("/v1/finance/expense-claims/{id}", operation_id="getExpenseClaim")
async def get_claim(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """查询当前单据与持久工作流，不依赖聊天记忆。"""
    return await get_store(request).get_expense_claim(who, id)


@router.put("/v1/finance/expense-claims/{id}", operation_id="updateExpenseClaim")
async def update_claim(
    request: Request, id: str, body: ExpenseUpdate, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """只替换可编辑版本，状态和身份不来自请求。"""
    require_write(request)
    return await get_store(request).update_expense_claim(who, id, body, key)


@router.post(
    "/v1/finance/invoice-allocations", status_code=201, operation_id="createInvoiceAllocation"
)
async def allocate(
    request: Request, body: AllocationRequest, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """创建版本化提议，提交时才原子占额。"""
    require_write(request)
    return await get_store(request).create_invoice_allocation(who, body, key)


@router.get("/v1/finance/invoice-allocations/{id}", operation_id="getInvoiceAllocation")
async def get_allocation(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """同时验证单据和发票可见权限后读取分配。"""
    return await get_store(request).get_invoice_allocation(who, id)


@router.post(
    "/v1/finance/invoice-allocations/{id}/release", operation_id="releaseInvoiceAllocation"
)
async def release_allocation(
    request: Request, id: str, body: VersionRequest, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """解除未提交的提议；已预留额度通过正式撤回或驳回释放。"""
    require_write(request, cancel=True)
    return await get_store(request).release_invoice_allocation(who, id, body.expected_version, key)


@router.get("/v1/finance/catalog/expense-categories", operation_id="listExpenseCategories")
async def categories(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """读取受控分类及对应制度版本，不能由模型拼造。"""
    return await get_store(request).list_expense_categories(
        who, legal_entity_id, cursor=cursor, limit=limit
    )


@router.get("/v1/finance/catalog/cost-centers", operation_id="listCostCenters")
async def centers(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
    kind: CenterKind = None,
) -> dict[str, Any]:
    """仅列当前员工允许使用的部门和项目。"""
    return await get_store(request).list_cost_centers(
        who, legal_entity_id, cursor=cursor, limit=limit, kind=kind
    )


@router.get("/v1/finance/review-queue", operation_id="listReviewQueue")
async def review_queue(
    request: Request,
    who: FinanceCaller,
    legal_entity_id: Company,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> dict[str, Any]:
    """读取实际分派的持久待办，页面或模型退出后仍可恢复。"""
    return await get_store(request).list_review_queue(
        who, legal_entity_id, cursor=cursor, limit=limit
    )


@router.post("/v1/finance/action-previews", status_code=201, operation_id="previewFinanceAction")
async def preview(
    request: Request, body: ActionPreview | ComplianceActionPreview, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """只生成固定版本计划，不占额度或产生批准。"""
    await guard_internal_target(request, who, body.target_type, body.target_id, body.action)
    funding = await funding_action_target(request, who, body.target_type, body.target_id)
    require_write(request, cancel=body.action in ({"reject", "withdraw"} if funding else {"approve", "reject", "withdraw"}))
    return await get_store(request).preview_finance_action(who, body, key)


@router.get("/v1/finance/action-plans/{id}", operation_id="getActionPlan")
async def action_plan(request: Request, id: str, who: FinanceCaller) -> dict[str, Any]:
    """查询原计划及实时可执行性，不延长原有效期。"""
    plan = await get_store(request).get_action_plan(who, id)
    await guard_internal_target(request, who, plan["target_type"], plan["target_id"], plan["action"])
    return plan


@router.post("/v1/finance/action-executions", status_code=202, operation_id="executeFinanceAction")
async def execute(
    request: Request, body: ActionExecution, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """必须真实人工来源；审批、版本、幂等和金额由领域层再次校验。"""
    if who.origin != "human":
        raise FinanceError("HUMAN_REQUIRED", 403, "请本人在人工确认入口作出业务决定")
    plan = await get_store(request).get_action_plan(who, body.plan_id)
    await guard_internal_target(request, who, plan["target_type"], plan["target_id"], plan["action"])
    funding = await funding_action_target(request, who, plan["target_type"], plan["target_id"])
    require_write(request, cancel=plan["action"] in ({"reject", "withdraw"} if funding else {"approve", "reject", "withdraw"}))
    return await get_store(request).execute_finance_action(who, body, key)


@router.post("/v1/finance/exceptions/{id}/resolutions", operation_id="resolveException")
async def resolve(
    request: Request, id: str, body: ExceptionResolution, key: IdempotencyKey, who: FinanceCaller
) -> dict[str, Any]:
    """附证据或受权处置告警，不绕过原件、越权及金额硬约束。"""
    require_write(request, cancel=True)
    return await get_store(request).resolve_exception(who, id, body, key)
