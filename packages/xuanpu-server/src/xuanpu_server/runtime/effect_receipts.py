"""外部效应意图与权威回执，不以批准或模型文字冒充下游结果。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from .effect_reconciliation import ReconciliationProof

from pydantic import Field

from xuanpu_server.work_tasks.models import Digest, FrozenModel, Identifier, Timestamp


class EffectReceipt(FrozenModel):
    """发送前即成立的恢复屏障；未知时永不自动重发。"""

    logical_action_id: Digest
    task_id: Identifier
    attempt_no: int
    call_id: str = Field(max_length=256)
    submission_id: str | None = None
    tool: str = Field(max_length=256)
    arguments_digest: Digest
    state: Literal['dispatch_intent', 'confirmed_success', 'confirmed_not_applied'] = 'dispatch_intent'
    created_at: Timestamp
    result_digest: Digest | None = None
    authority: str | None = None
    verification_version: int = Field(default=1, ge=1)


class ReceiptVerifier(Protocol):
    """平台登记的下游成功回执验证器，未登记即未知。"""

    async def verify_response(self, intent: EffectReceipt, response: str) -> ReconciliationProof:
        """只接受该下游契约的确定回执，不能以HTTP状态码直接断言业务成功。"""
        ...
