"""仅服务端登记的权威查询可以核实未知效应，永不自动重发。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import Field

from xuanpu_server.errors import ConflictError, NotFoundError
from xuanpu_server.runtime.effect_receipts import EffectReceipt
from xuanpu_server.work_tasks.models import FrozenModel
from xuanpu_server.work_tasks.store import require_task, save_record

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.service import WorkspaceService


class ReconciliationProof(FrozenModel):
    """适配器的结构化权威回执，不接受用户或模型提供的自由文本。"""

    state: Literal['confirmed_success', 'confirmed_not_applied']
    authority: str = Field(min_length=1, max_length=128)
    evidence_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class AuthorityAdapter(Protocol):
    """服务端实现只读状态查询，登记时固定下游和核验契约。"""

    async def verify(self, receipt: EffectReceipt) -> ReconciliationProof:
        """无法确定时抛错，不能把网络错误视为未执行。"""
        ...


class EffectReconciliation:
    """保存核实结论，原unknown业务终态和证据永远保留。"""

    def __init__(self, workspace: WorkspaceService, *, now: Callable[[], float],
                 adapters: dict[str, AuthorityAdapter] | None = None) -> None:
        """默认没有可自动核实的连接器；部署方只能登记固定平台适配器。"""
        self.workspace, self.now = workspace, now
        self.adapters = dict(adapters or {})

    async def verify(self, who: Principal, task_id: str, action_id: str, *, expected_version: int) -> EffectReceipt:
        """查询在锁外，提交在本人事务CAS中；新后续任务须另行明确交办。"""
        async with self.workspace.transaction(who, 'work-task:exec:' + task_id) as state:
            task = require_task(state, task_id)
            if task.state_version != expected_version or task.status != 'outcome_unknown':
                raise ConflictError('只能核实当前版本的未知终态')
            value = state.task_effects.get(action_id)
            if value is None or value['task_id'] != task_id:
                raise NotFoundError('效应回执不存在')
            receipt = EffectReceipt.model_validate_json(json.dumps(value))
        adapter = self.adapters.get(receipt.tool)
        if adapter is None:
            raise ConflictError('该连接器没有登记权威核实接口，请人工核实')
        proof = await adapter.verify(receipt)
        async with self.workspace.transaction(who, 'work-task:exec:' + task_id) as state:
            current = require_task(state, task_id)
            if current.state_version != expected_version or current.status != 'outcome_unknown':
                raise ConflictError('核实期间任务版本已变化')
            updated = receipt.model_copy(update={'state': proof.state, 'authority': proof.authority,
                'result_digest': proof.evidence_digest, 'verification_version': receipt.verification_version + 1})
            state.task_effects[action_id] = updated.model_dump(mode='json')
            save_record(state, current.model_copy(update={'state_version': current.state_version + 1,
                'updated_at': max(self.now(), current.updated_at)}), 'effect_reconciled')
            return updated
