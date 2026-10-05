"""对话草稿的持久确认入口；同一个决定只执行一次，模块与对话共用领域服务。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from xuanpu_server.errors import InvalidRequestError, NotFoundError
from xuanpu_server.reminders.models import ReminderInput
from xuanpu_server.workspace.models import validate_data

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform
    from xuanpu_server.tenancy import Principal


class ConfirmBody(BaseModel):
    """本人可在确认前修改字段，能力类型取自已持久化的模型草稿。"""
    model_config = ConfigDict(extra="forbid", strict=True)
    data: dict[str, Any]


def assistant_actions_router(platform: Platform) -> APIRouter:
    """恢复、确认、取消草稿都须通过当前员工身份。"""
    router = APIRouter(prefix="/v1/assistant/proposals")
    service = platform.workspace

    async def proposal(who: Principal, request_id: str) -> dict[str, Any]:
        """只允许操作者本人已产生的写入建议，不能凭空构造操作。"""
        record = await service.find_message(who, request_id + ":assistant")
        route = record["metadata"].get("route", {}) if record else {}
        if not (route.get("workspace_draft") or route.get("reminder_draft")):
            raise NotFoundError("待确认操作不存在")
        return dict(route)

    async def status(who: Principal, request_id: str) -> dict[str, Any]:
        """持久终态决定是否恢复按钮，不能重复创建已经确认的记录。"""
        route = await proposal(who, request_id)
        receipt = await service.proposal_result(who, request_id)
        if receipt is not None:
            return {**receipt, "proposal_id": request_id}
        for state in ("confirmed", "cancelled"):
            terminal = await service.find_message(who, request_id + ":" + state)
            if terminal:
                return {"state": state, "result": terminal["metadata"].get("result"), "proposal_id": request_id}
        return {"state": "pending", "route": route, "proposal_id": request_id}

    @router.get("")
    async def pending(who: Caller) -> dict[str, Any]:
        """未确认草稿同时作为个人待办入口。"""
        found = await service.pending_proposals(who)
        return {"items": [{"proposal_id": item["request_id"].removesuffix(":assistant"),
                           "route": item["metadata"]["route"], "created_at": item["created_at"]}
                          for item in found]}

    @router.get("/{request_id}")
    async def read(request_id: str, who: Caller) -> dict[str, Any]:
        """刷新后恢复本人原来的草稿或确认结果。"""
        return await status(who, request_id)

    @router.post("/{request_id}/confirm")
    async def confirm(request_id: str, body: ConfirmBody, who: Caller) -> dict[str, Any]:
        """确认时只调用既有领域服务，不允许模型直接产生写操作。"""
        prior = await status(who, request_id)
        if prior["state"] == "confirmed":
            return prior
        if prior["state"] != "pending":
            raise InvalidRequestError("该操作已取消")
        route = await proposal(who, request_id)
        if route.get("workspace_draft"):
            kind = route["workspace_draft"]["kind"]
            checked = validate_data(kind, body.data)
            if kind == "file":
                if any(key in body.data for key in ("size", "mime_type", "current_revision")):
                    raise InvalidRequestError("不能在确认时指定文件内容字段")
                checked = {key: checked[key] for key in ("name", "parent_id", "matter_id", "tags", "source_url")}
        else:
            reminder = ReminderInput.model_validate(body.data)
            checked = reminder.model_dump()
        if route.get("workspace_draft"):
            result = await service.create(who, kind, checked, request_id + ":create", proposal_id=request_id)
            title = result["data"].get("title", result["data"].get("name", "记录"))
        else:
            record = await platform.reminders.create(who, reminder, request_id + ":create", proposal_id=request_id)
            result = {"reminder_id": record.reminder_id}
            title = reminder.title
        await service.append_message(who, "assistant", "已保存：" + title, request_id + ":confirmed",
                                     metadata={"proposal_id": request_id, "result": result})
        return {"state": "confirmed", "result": result, "proposal_id": request_id}

    @router.post("/{request_id}/cancel")
    async def cancel(request_id: str, who: Caller) -> dict[str, Any]:
        """取消建议只留本人对话记录，不创建任何业务数据。"""
        prior = await status(who, request_id)
        if prior["state"] == "cancelled":
            return prior
        await service.reserve_proposal(who, request_id, "cancel", {})
        await service.append_message(who, "assistant", "已取消这项待确认操作。", request_id + ":cancelled",
                                     metadata={"proposal_id": request_id})
        return {"state": "cancelled", "proposal_id": request_id}

    return router
