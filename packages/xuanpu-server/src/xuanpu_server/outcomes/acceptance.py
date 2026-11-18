"""验收批次与人工核实观察（R4-F0）：业务核实与技术完成分开统计。

- 批次冻结能力修订、验收标准与样本任务（须是绑定到这个能力修订的任务）；建后不可改，
  变更须另建新批次并写明取代哪一批，旧批次与其失败样本照旧可查；
- 观察只追加：每个样本最新一条有效观察为准，纠正另记一条并指向被纠正的那条；
  同一操作号重复提交回原结果；观察不修改任务、交接或审批的任何状态（不是代签收）；
- 核实通过率 = 通过样本 / 已有结论（通过或失败）的样本；待验、阻塞单列，不从分母隐去也不当 0。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from xuanpu_server.capabilities.models import AcceptanceBatch, Observation
from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic import JsonValue

    from xuanpu_server.capabilities.bindings import BindingStore
    from xuanpu_server.capabilities.service import CapabilityService
    from xuanpu_server.tenancy import Principal


class BatchInput(BaseModel):
    """建批次：能力修订、名称、验收标准、样本任务（1–500）、证据类别、可选取代的旧批次。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    capability_id: str = Field(min_length=1, max_length=128)
    capability_revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    criteria: str = Field(min_length=1, max_length=2000)
    samples: list[str] = Field(min_length=1, max_length=500)
    evidence_kind: Literal['synthetic', 'real_model', 'pilot'] = 'synthetic'
    supersedes: str | None = None


class ObservationInput(BaseModel):
    """登记一条核实结论与证据引用；纠正时写明被纠正的观察。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    verdict: Literal['passed', 'failed', 'blocked']
    evidence_ref: str = Field(min_length=1, max_length=500)
    note: str = Field(default='', max_length=1000)
    supersedes: str | None = None


class AcceptanceService:
    """批次与观察的登记与汇总（仅管理员）。"""

    def __init__(self, capabilities: CapabilityService, bindings: BindingStore) -> None:
        """复用能力服务的管理员复核、仓储、回执与时钟。"""
        self.capabilities, self.bindings = capabilities, bindings

    async def create(self, who: Principal, body: BatchInput) -> dict[str, JsonValue]:
        """冻结一个批次；样本须是绑定到这个能力修订的任务。"""
        s = self.capabilities
        who = await s.admin(who, mutation=True)
        tenant = who.require_tenant()
        if len(set(body.samples)) != len(body.samples):
            raise InvalidRequestError('样本任务不能重复')
        bound = {b.ref_id for b in await self.bindings.tasks(tenant)
                 if (b.capability_id, b.capability_revision) == (body.capability_id, body.capability_revision)}
        if not set(body.samples) <= bound:
            raise InvalidRequestError('样本里有不属于这个能力修订的任务')
        payload = {'kind': 'acceptance.batch', **body.model_dump(mode='json')}
        async with s.store.transaction(tenant, participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            state = tx.state.capabilities
            if body.supersedes is not None and body.supersedes not in state.batches:
                raise NotFoundError('被取代的批次不存在')
            batch = AcceptanceBatch(
                tenant_id=tenant, id=new_id('acb'), created_at=s.now(), capability_id=body.capability_id,
                capability_revision=body.capability_revision, name=body.name, criteria=body.criteria,
                samples=tuple(body.samples), evidence_kind=body.evidence_kind,
                supersedes=body.supersedes, created_by=who.actor)
            state.batches[batch.id] = batch
            tx.emit('acceptance.batch_created', batch.id, batch.version, actor=who.actor, at=s.now())
            result = self._view(batch, state.observations.values())
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def observe(self, who: Principal, batch_id: str, body: ObservationInput) -> dict[str, JsonValue]:
        """追加一条观察；纠正须指向同一样本的现行观察。"""
        s = self.capabilities
        who = await s.admin(who, mutation=True)
        payload = {'kind': 'acceptance.observe', 'batch_id': batch_id, **body.model_dump(mode='json')}
        async with s.store.transaction(who.require_tenant(), participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            state = tx.state.capabilities
            batch = state.batches.get(batch_id)
            if batch is None:
                raise NotFoundError('批次不存在')
            if body.task_id not in batch.samples:
                raise InvalidRequestError('这个任务不在批次样本里')
            current = _latest(batch_id, state.observations.values()).get(body.task_id)
            if (current is None) != (body.supersedes is None) or (current is not None and current.id != body.supersedes):
                raise ConflictError('这个样本已有结论：纠正时须指明被纠正的那条现行观察')
            item = Observation(tenant_id=batch.tenant_id, id=new_id('obs'), created_at=s.now(), batch_id=batch_id,
                               task_id=body.task_id, verdict=body.verdict,
                               evidence_ref=body.evidence_ref, note=body.note, observer=who.actor,
                               supersedes=body.supersedes)
            state.observations[item.id] = item
            tx.emit('acceptance.observed', item.id, item.version, actor=who.actor, at=s.now())
            result: dict[str, JsonValue] = {'id': item.id, 'task_id': item.task_id, 'verdict': item.verdict, 'supersedes': item.supersedes,
                      'created_at': item.created_at}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def detail(self, who: Principal, batch_id: str) -> dict[str, JsonValue]:
        """批次详情：标准、样本数、每样本现行结论与核实通过率（待验、阻塞单列）。"""
        s = self.capabilities
        who = await s.admin(who)
        state, _ = await s.store.capability_snapshot(who.require_tenant())
        batch = state.batches.get(batch_id)
        if batch is None:
            raise NotFoundError('批次不存在')
        return self._view(batch, state.observations.values())

    @staticmethod
    def _view(batch: AcceptanceBatch, observations: Iterable[Observation]) -> dict[str, JsonValue]:
        """统计口径：通过率只除以已有结论的样本；未核实的样本标 not_checked。"""
        latest = _latest(batch.id, observations)
        verdicts: dict[str, JsonValue] = {
            task: (latest[task].verdict if task in latest else 'not_checked') for task in batch.samples}
        counts: dict[str, int] = {v: sum(1 for x in verdicts.values() if x == v)
                                  for v in ('passed', 'failed', 'blocked', 'not_checked')}
        decided = counts['passed'] + counts['failed']
        return {'id': batch.id, 'capability_id': batch.capability_id, 'capability_revision': batch.capability_revision,
                'name': batch.name, 'criteria': batch.criteria, 'evidence_kind': batch.evidence_kind,
                'supersedes': batch.supersedes, 'samples': len(batch.samples), 'counts': dict(counts.items()),
                'verification_rate': round(counts['passed'] / decided, 4) if decided else None,
                'verdicts': verdicts, 'created_at': batch.created_at}


def _latest(batch_id: str, observations: Iterable[Observation]) -> dict[str, Observation]:
    """每个样本的现行观察（没有被别的观察取代的那条）。"""
    items = [o for o in observations if o.batch_id == batch_id]
    superseded = {o.supersedes for o in items if o.supersedes}
    return {o.task_id: o for o in items if o.id not in superseded}
