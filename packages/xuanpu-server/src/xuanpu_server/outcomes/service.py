"""能力运行结果与资源统计（R4-F0）：按调用绑定归因，按受理时间选队列，未知不填 0。

- 正本是调用绑定（哪次受理用了哪个能力修订与发布）与 R2 任务账本；统计每次现算，可随时重建，
  不另存聚合（因此没有「重建中」的部分状态）；
- 队列按受理时间（``accepted_at``）选，状态随观察时间变化；响应固定窗口、队列口径、观察时间与
  聚合版本，避免用同一时窗的完成数去除另一队列的受理数；
- 技术完成率 = 队列里 succeeded 的任务 / 队列全部任务，其他状态同时列出，不从分母隐去；
- token：已结算的按实际用量；仍占用的预留只给上界、单列；没有预留记录的任务计入「未记录」，不当 0；
- 费用：没有锁定的价格表，一律 ``price_unavailable``、金额为空，绝不补 0 元；
- 员工只看本人记录；管理员只看按能力与修订的计数与用量，不看任务正文、标题或失败原因。
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import InvalidRequestError
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.execution_models import BudgetReservation
from xuanpu_server.work_tasks.models import WorkTaskRecord

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

    from xuanpu_server.capabilities.bindings import BindingStore, InvocationBinding
    from xuanpu_server.capabilities.service import CapabilityService
    from xuanpu_server.workspace.service import WorkspaceService

logger = logging.getLogger(__name__)
AGGREGATION_VERSION = 1
STATUSES = ('accepted', 'running', 'waiting_user', 'succeeded', 'failed', 'cancelled', 'outcome_unknown',
            'interrupted')


class OutcomeService:
    """能力结果统计（员工本人与管理员两种视图）。"""

    def __init__(self, capabilities: CapabilityService, workspace: WorkspaceService, bindings: BindingStore,
                 *, now: Callable[[], float]) -> None:
        """依赖由平台注入。"""
        self.capabilities, self.workspace, self.bindings, self.now = capabilities, workspace, bindings, now

    async def _facts(self, binding: InvocationBinding) -> dict[str, Any]:
        """一笔绑定任务的状态与资源；读不到（员工已停用、记录缺失）如实标出。"""
        who = Principal(role='tenant_member', tenant_id=binding.tenant_id, user_id=binding.user_id, key_id=None)
        try:
            # 按任务键读取（PG 只加载与键相关的任务与预留）
            async with self.workspace.transaction(who, 'work-task:budget:' + binding.ref_id) as state:
                raw = state.work_tasks.get(binding.ref_id)
                reservations = [BudgetReservation.model_validate_json(json.dumps(v))
                                for v in state.task_reservations.values() if v.get('task_id') == binding.ref_id]
        except Exception as exc:  # noqa: BLE001 —— 读不到即「不可读」，统计里单列，不当成功或失败
            logger.warning('能力结果归因读不到任务 %s：%s', binding.ref_id, type(exc).__name__)
            return {'binding': binding, 'status': 'unreadable', 'accepted_at': binding.created_at}
        if raw is None:
            return {'binding': binding, 'status': 'missing', 'accepted_at': binding.created_at}
        task = WorkTaskRecord.model_validate_json(json.dumps(raw))
        settled = [r for r in reservations if r.state == 'settled']
        held = [r for r in reservations if r.state == 'held']
        return {'binding': binding, 'status': task.status, 'accepted_at': task.accepted_at,
                'finished_at': task.finished_at,
                'tokens_settled': sum(r.input_tokens + r.output_tokens for r in settled) if reservations else None,
                'tokens_held_upper': sum(r.upper_bound for r in held) if reservations else None}

    def _summary(self, facts: list[dict[str, Any]], since: float | None, until: float | None) -> dict[str, JsonValue]:
        """队列统计：状态计数、技术完成率、token（实际/上界/未记录分列）、费用口径。"""
        counts = Counter(f['status'] for f in facts)
        total = len(facts)
        recorded = [f for f in facts if f.get('tokens_settled') is not None]
        totals: dict[str, JsonValue] = {'tasks': total, **{s: counts.get(s, 0) for s in STATUSES},
                                        'unreadable': counts.get('unreadable', 0), 'missing': counts.get('missing', 0)}
        return {
            'window_start': since, 'window_end': until, 'cohort_basis': 'accepted_at',
            'observed_at': self.now(), 'aggregation_version': AGGREGATION_VERSION, 'evidence_kind': 'runtime',
            'totals': totals,
            'technical_completion': {'succeeded': counts.get('succeeded', 0), 'cohort': total,
                                     'rate': round(counts.get('succeeded', 0) / total, 4) if total else None},
            'tokens': {'settled': sum(f['tokens_settled'] for f in recorded),
                       'held_upper_bound': sum(f.get('tokens_held_upper') or 0 for f in recorded),
                       'tasks_with_records': len(recorded), 'tasks_without_records': total - len(recorded)},
            'cost': {'status': 'price_unavailable', 'amount': None, 'currency': None},
            'business_verification': {'status': 'not_checked'},
        }

    @staticmethod
    def _window(facts: list[dict[str, Any]], since: float | None, until: float | None) -> list[dict[str, Any]]:
        """按受理时间选队列 [since, until)。"""
        if since is not None and until is not None and until <= since:
            raise InvalidRequestError('时间窗口不合法')
        return [f for f in facts if (since is None or f['accepted_at'] >= since)
                and (until is None or f['accepted_at'] < until)]

    async def mine(self, who: Principal, *, since: float | None = None, until: float | None = None,
                   limit: int = 50, offset: int = 0) -> dict[str, JsonValue]:
        """本人能力任务：总数独立于当前页，列表只给元数据。"""
        tenant, user = await self.workspace.require_employee(who)
        if not 1 <= limit <= 100 or offset < 0:
            raise InvalidRequestError('分页参数无效')
        facts = self._window([await self._facts(b) for b in await self.bindings.tasks(tenant, user)], since, until)
        page: list[JsonValue] = [{
            'task_id': f['binding'].ref_id, 'capability_id': f['binding'].capability_id,
            'capability_revision': f['binding'].capability_revision, 'release_id': f['binding'].release_id,
            'status': f['status'], 'accepted_at': f['accepted_at'], 'finished_at': f.get('finished_at'),
            'tokens_settled': f.get('tokens_settled')} for f in facts[offset:offset + limit]]
        return {**self._summary(facts, since, until), 'items': page,
                'next_offset': offset + limit if offset + limit < len(facts) else None}

    async def admin(self, who: Principal, *, since: float | None = None,
                    until: float | None = None) -> dict[str, JsonValue]:
        """管理员：按能力与修订的计数与用量，不含任务正文、标题、失败原因或员工明细。"""
        who = await self.capabilities.admin(who)
        tenant = who.require_tenant()
        facts = self._window([await self._facts(b) for b in await self.bindings.tasks(tenant)], since, until)
        groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
        for fact in facts:
            groups.setdefault((fact['binding'].capability_id, fact['binding'].capability_revision), []).append(fact)
        by_capability: list[JsonValue] = [
            {'capability_id': c, 'capability_revision': r, **self._summary(items, since, until)}
            for (c, r), items in sorted(groups.items())]
        return {**self._summary(facts, since, until), 'capabilities': by_capability}
