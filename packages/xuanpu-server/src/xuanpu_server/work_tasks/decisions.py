"""任务决定是严格的本人输入，不能携带任意内核命令或写入权限。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, field_validator

from xuanpu_server.work_tasks.models import FrozenModel, Identifier, Timestamp


class ApprovalValue(FrozenModel):
    """本人只对明确的工具待办作出允许或拒绝。"""

    granted: bool
    reason: str = Field(default="", max_length=1000)


class ToolApprovalDecision(FrozenModel):
    """工具批准继续复用R0的持久核销回执。"""

    kind: Literal["tool_approval"]
    request_id: Identifier
    state_version: int = Field(ge=1)
    decisions: dict[Identifier, ApprovalValue] = Field(min_length=1, max_length=256)


class ActionChoice(FrozenModel):
    """选择或编辑已存在行动项，不能创建新身份或扩大分派范围。"""

    action_id: Identifier
    accepted: bool
    title: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"\S")
    description: str | None = Field(default=None, max_length=20000)
    due_at: Timestamp | None = None
    start_at: Timestamp | None = None
    end_at: Timestamp | None = None
    remind_at: Timestamp | None = None


class OfficeActionsDecision(FrozenModel):
    """本人逐项决定，空数组表示全部取消；真实候选只能来自服务器。"""

    kind: Literal["office_actions"]
    request_id: Identifier
    state_version: int = Field(ge=1)
    choices: tuple[ActionChoice, ...] = Field(default=(), max_length=32)

    @field_validator("choices", mode="before")
    @classmethod
    def immutable_choices(cls, value: Any) -> Any:
        """严格验证数组内的决定，再保存为不可变序列。"""
        return tuple(value) if isinstance(value, list) else value


TaskDecision = Annotated[ToolApprovalDecision | OfficeActionsDecision, Field(discriminator="kind")]
