"""协调工作单元只在拥有它的短事务内有效。"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, JsonValue

from xuanpu_server.coordination.outbox import CoordinationEvent
from xuanpu_server.coordination.receipts import CoordinationReceipt, fingerprint, receipt_key
from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.organization.models import OrganizationState
from xuanpu_server.organization.user_profiles import ProfileUpdate, validate_profiles
from xuanpu_server.shared_matters.models import SharedState


class CoordinationState(BaseModel):
    """实体、回执、事件和确认记录的一次提交快照。"""
    organization: OrganizationState = Field(default_factory=OrganizationState)
    shared: SharedState = Field(default_factory=SharedState)
    receipts: dict[str, CoordinationReceipt] = Field(default_factory=dict)
    events: dict[str, CoordinationEvent] = Field(default_factory=dict)
    acknowledgements: dict[str, set[str]] = Field(default_factory=dict)


@dataclass
class CoordinationTransaction:
    """不可转交给后台任务的事务工作单元。"""
    tenant_id: str
    participants: tuple[str, ...]
    state: CoordinationState
    participant_epochs: dict[str, int] = field(default_factory=dict)
    profile_updates: dict[str, ProfileUpdate] = field(default_factory=dict)

    def stage_profile(self, update: ProfileUpdate) -> None:
        """只登记写集，成功退出协调事务时与实体、回执、事件一并提交。"""
        previous = self.profile_updates.get(update.user_id)
        if previous is not None and previous != update:
            raise ConflictError('同一批不能多次改写同一员工资料')
        self.profile_updates[update.user_id] = update
        try:
            validate_profiles(self)
        except BaseException:
            self.profile_updates.pop(update.user_id)
            if previous is not None:
                self.profile_updates[update.user_id] = previous
            raise

    @property
    def organization(self) -> OrganizationState:
        """返回本次待提交的组织快照，仓储不得自行提交。"""
        return self.state.organization

    @property
    def shared(self) -> SharedState:
        """返回与组织变更共用提交边界的共享快照。"""
        return self.state.shared

    def receipt(self, actor: str, request_id: str, payload: dict[str, JsonValue]) -> dict[str, JsonValue] | None:
        """先检查请求是否重放；不同参数不能复用旧操作号。"""
        self._request_id(request_id)
        old = self.state.receipts.get(receipt_key(actor, request_id))
        if old is None:
            return None
        if old.fingerprint != fingerprint(payload):
            raise ConflictError('操作号已用于其他请求')
        return copy.deepcopy(old.result)

    def record_receipt(self, actor: str, request_id: str, payload: dict[str, JsonValue], result: dict[str, JsonValue]) -> None:
        """记录第一次结果，后续相同请求永不替换原回执。"""
        if self.receipt(actor, request_id, payload) is not None:
            return
        self.state.receipts[receipt_key(actor, request_id)] = CoordinationReceipt(
            actor=actor, request_id=request_id, fingerprint=fingerprint(payload), result=copy.deepcopy(result))

    def emit(self, kind: str, entity_id: str, version: int, *, actor: str, at: float,
             data: dict[str, JsonValue] | None = None) -> CoordinationEvent:
        """同一实体版本的同种事件使用稳定ID，重试不重复发送。"""
        semantic = json.dumps([self.tenant_id, kind, entity_id, version], ensure_ascii=False)
        key = 'evt_' + hashlib.sha256(semantic.encode()).hexdigest()
        if key not in self.state.events:
            self.state.events[key] = CoordinationEvent(id=key, tenant_id=self.tenant_id,
                kind=kind, entity_id=entity_id, entity_version=version, actor=actor, at=at, data=data or {})
        return self.state.events[key]

    def pending_events(self, consumer: str, *, limit: int = 100) -> list[CoordinationEvent]:
        """按事件逐条重试，不用最大序号跳过晚提交事件。"""
        if not 1 <= limit <= 100:
            raise InvalidRequestError('事件页大小须为1至100')
        acknowledged = self.state.acknowledgements.get(consumer, set())
        return sorted((e for e in self.state.events.values() if e.id not in acknowledged),
                      key=lambda e: (e.at, e.id))[:limit]

    def ack(self, consumer: str, event_id: str) -> None:
        """只确认明确完成的一个事件，重复确认无副作用。"""
        if event_id not in self.state.events:
            raise NotFoundError('事件不存在')
        self.state.acknowledgements.setdefault(consumer, set()).add(event_id)

    @staticmethod
    def _request_id(request_id: str) -> None:
        """防止无界操作号和空操作号。"""
        if not request_id.strip() or len(request_id) > 160:
            raise InvalidRequestError('操作号须为1至160个字符')
