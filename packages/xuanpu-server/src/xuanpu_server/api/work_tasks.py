"""工作任务的受理、本人补读和决定接口，复用认证与CSRF。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Query

from xuanpu_server.api.auth import Caller  # noqa: TC001 — FastAPI解析
from xuanpu_server.work_tasks.decisions import TaskDecision  # noqa: TC001 — FastAPI解析
from xuanpu_server.work_tasks.models import (  # noqa: TC001 — FastAPI解析
    CapabilityTaskInput,
    WorkTaskInput,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def work_tasks_router(platform: Platform) -> APIRouter:
    """路由只翻译输入，不在HTTP处理器中持有模型执行。"""
    router = APIRouter(prefix="/v1/work-tasks")
    service = platform.work_tasks

    @router.post("", status_code=202)
    async def accept(body: WorkTaskInput, who: Caller) -> dict[str, Any]:
        """持久受理后返回任务标识，不能提前声称完成。"""
        return await service.public_record(who, await service.accept(who, body))

    @router.post('/from-capability', status_code=202)
    async def from_capability(body: CapabilityTaskInput, who: Caller) -> dict[str, Any]:
        """无附件能力同样先受理，再由持久执行器完成。"""
        return await service.public_record(who, await service.accept_capability(who, body))

    @router.get("")
    async def list_tasks(
        who: Caller, limit: Annotated[int, Query(ge=1, le=100)] = 100, after_id: str | None = None,
    ) -> dict[str, Any]:
        """稳定分页读取本人任务。"""
        tasks = await service.store.list(who, limit=limit, after_id=after_id)
        return {"items": [await service.public_record(who, task) for task in tasks]}

    @router.get("/{task_id}")
    async def get(task_id: str, who: Caller) -> dict[str, Any]:
        """刷新查询始终来自持久记录。"""
        return await service.public_record(who, await service.store.get(who, task_id))

    @router.get("/{task_id}/events")
    async def events(
        task_id: str, who: Caller, after_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> dict[str, Any]:
        """按持久游标补读，连接断开不取消任务。"""
        entries = await service.store.events(who, task_id, after_id=after_id, limit=limit)
        return {"items": [entry.model_dump(mode="json") for entry in entries]}

    @router.post("/{task_id}/cancel", status_code=202)
    async def cancel(task_id: str, who: Caller) -> dict[str, Any]:
        """返回停止请求及实际状态，running不伪装成已停止。"""
        task = await service.cancel(who, task_id)
        return {"task_id": task_id, "cancel_requested": task.cancel_requested, "status": task.status}

    @router.post("/{task_id}/decisions")
    async def decide(task_id: str, body: TaskDecision, who: Caller) -> dict[str, Any]:
        """本人决定经服务层复核和幂等回执处理。"""
        return await service.public_record(who, await service.decide(who, task_id, body))

    return router
