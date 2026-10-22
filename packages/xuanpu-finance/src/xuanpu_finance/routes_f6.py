"""F6准备报告的签名只读HTTP入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from xuanpu_finance.dependencies import FinanceCaller, get_store
from xuanpu_finance.routes import Company  # noqa: TC001 — FastAPI运行时解析

router = APIRouter()


@router.get("/v1/finance/readiness", operation_id="getFinanceReadiness")
async def readiness(
    request: Request, who: FinanceCaller, legal_entity_id: Company
) -> dict[str, Any]:
    """当前配置及实时运行态不构成正式验收或逐单执行许可。"""
    data = await get_store(request).get_finance_readiness(who, legal_entity_id)
    runtime: dict[str, Any] = {
        "mode": request.app.state.mode,
        "instance_lock_lost": request.app.state.instance_lock_lost,
    }
    for name in ("worker", "settlement", "compliance", "notification"):
        error_name = "worker_error" if name == "worker" else name + "_worker_error"
        task_name = "worker_task" if name == "worker" else name + "_task"
        task = getattr(request.app.state, task_name, None)
        runtime[name] = (
            "failed"
            if getattr(request.app.state, error_name, None) is not None
            else "running"
            if task is not None and not task.done()
            else "not_started"
        )
    return {**data, "legal_entity_id": legal_entity_id, "runtime": runtime}
