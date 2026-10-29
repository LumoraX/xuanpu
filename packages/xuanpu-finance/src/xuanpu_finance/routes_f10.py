"""内部试用检查签名只读入口，配置摘要与实时运行情况分别核对。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.readiness.internal_live import runtime_checks
from xuanpu_finance.routes import Company  # noqa: TC001 — FastAPI运行时解析

if TYPE_CHECKING:
    from starlette.datastructures import State

router = APIRouter()


def task_status(state: State, task_name: str, error_name: str) -> str:
    """实际任务存在且未结束才报告running，诊断正文不进入响应。"""
    task = getattr(state, task_name, None)
    if getattr(state, error_name, None) is not None:
        return "failed"
    if task is None:
        return "not_started"
    return "failed" if task.done() else "running"


def internal_runtime(state: State) -> dict[str, Any]:
    """仅暴露固定状态；worker或注册表任一实际装配均为外部存在。"""
    external = any(
        getattr(state, name, None) is not None
        for name in (
            "settlement_worker",
            "settlement_providers",
            "compliance_worker",
            "compliance_providers",
            "settlement_task",
            "compliance_task",
        )
    )
    return {
        "mode": state.mode,
        "instance_lock_lost": state.instance_lock_lost,
        "receipt_worker": task_status(state, "worker_task", "worker_error"),
        "notification_worker": task_status(state, "notification_task", "notification_worker_error"),
        "external_workers": "present" if external else "absent",
    }


@router.get("/v1/finance/internal-records-readiness", operation_id="getInternalRecordsReadiness")
async def internal_records_readiness(
    request: Request, who: FinanceCaller, legal_entity_id: Company
) -> dict[str, Any]:
    """完整配置仍只报告not_verified，禁止内部核对提升正式或执行资格。"""
    if request.app.state.profile != "internal_records":
        raise FinanceError("INTERNAL_RECORDS_ONLY", 403, "当前检查仅允许内部记录实例")
    data = await get_store(request).get_internal_records_readiness(who, legal_entity_id)
    runtime = internal_runtime(request.app.state)
    checks = [*data["checks"], *runtime_checks(runtime)]
    return {
        **data,
        "runtime": runtime,
        "checks": checks,
        "status": "blocked" if any(c["status"] == "blocked" for c in checks) else "not_verified",
    }
