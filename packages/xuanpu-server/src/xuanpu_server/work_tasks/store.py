"""工作任务存储复用已验证的内存和PostgreSQL员工原子事务。"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.work_tasks.models import (
    ArtifactReceipt,
    WorkTaskEvent,
    WorkTaskRecord,
    needs_recovery,
)
from xuanpu_server.work_tasks.rules import advance
from xuanpu_server.workspace.rules import audit, get_item

if TYPE_CHECKING:
    import builtins

    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.models import WorkspaceState
    from xuanpu_server.workspace.service import WorkspaceService


def parse_record(value: dict[str, Any]) -> WorkTaskRecord:
    """按JSON模式重新验证持久记录，包括不可变元组。"""
    return WorkTaskRecord.model_validate_json(json.dumps(value))


def require_task(state: WorkspaceState, task_id: str) -> WorkTaskRecord:
    """员工事务快照已限定本人范围，未知标识统一404。"""
    if task_id not in state.work_tasks:
        raise NotFoundError("工作任务不存在")
    return parse_record(state.work_tasks[task_id])


def validate_sources(state: WorkspaceState, task: WorkTaskRecord) -> None:
    """受理锁内核对正本版本摘要、回收状态和本人事项。"""
    if task.matter_id is not None:
        get_item(state, task.matter_id, kind="matter", active=True)
    seen: set[str] = set()
    for source in task.inputs:
        if source.item_id in seen:
            raise InvalidRequestError("同一文件不能重复作为输入")
        seen.add(source.item_id)
        item = get_item(state, source.item_id, kind="file", active=True)
        version = next((entry for entry in state.versions.get(source.item_id, [])
                        if entry["revision"] == source.revision), None)
        if item["data"]["source_url"] is not None or version is None:
            raise NotFoundError("输入文件正本或版本不存在")
        if any(version[key] != getattr(source, key) for key in ("sha256", "size", "mime_type")):
            raise ConflictError("输入文件与已核对的版本不一致")


def save_record(state: WorkspaceState, task: WorkTaskRecord, event_kind: str) -> None:
    """记录与单条有界进展在同一员工事务中更新，不写正文审计。"""
    state.work_tasks[task.task_id] = task.model_dump(mode="json")
    state.work_task_events.append(WorkTaskEvent(
        event_id=max((int(entry["event_id"]) for entry in state.work_task_events), default=0) + 1,
        task_id=task.task_id, state_version=task.state_version, at=task.updated_at,
        event_kind=event_kind, status=task.status, error_code=task.error_code,
    ).model_dump(mode="json"))
    audit(state, task.actor, "work_task_" + event_kind, task.updated_at,
          {"id": task.task_id, "kind": "work_task", "status": task.status})


class WorkTaskStore:
    """复用员工事务而非另建互斥层，输入固定和领域写入共用锁顺序。"""

    def __init__(self, workspace: WorkspaceService) -> None:
        """注入同一工作区服务，生产后端和内存后端遵循相同原子契约。"""
        self.workspace = workspace
        self.max_tasks = 1000
        self.durable_config: Any = None

    async def create_or_replay(
        self, who: Principal, task: WorkTaskRecord
    ) -> tuple[WorkTaskRecord, bool]:
        """先提交accepted、输入固定引用和事件，再允许启动执行。"""
        tenant, user = await self.workspace.require_employee(who)
        if (task.tenant_id, task.user_id, task.actor) != (tenant, user, who.actor):
            raise InvalidRequestError("任务执行身份不匹配")
        async with self.workspace.transaction(who, "work-task:accept") as state:
            old = next((parse_record(value) for value in state.work_tasks.values()
                        if value["request_id"] == task.request_id), None)
            if old is not None:
                if old.input_fingerprint != task.input_fingerprint:
                    raise ConflictError("同一请求标识对应的交办内容不同")
                return old, False
            if task.task_id in state.work_tasks or task.status != "accepted" or task.state_version != 1:
                raise ConflictError("工作任务标识或初始状态无效")
            if len(state.work_tasks) >= self.max_tasks:
                raise ConflictError("个人工作任务数量已达上限")
            validate_sources(state, task)
            if task.recovery_policy_version == 2:
                from .execution_store import initialize

                limits = self.durable_config
                task = initialize(state, task, who, max_attempts=limits.max_attempts if limits else 3,
                    recovery_seconds=limits.recovery_seconds if limits else 1800.)
            save_record(state, task, "accepted")
            return task, True

    async def get(self, who: Principal, task_id: str) -> WorkTaskRecord:
        """读取本人持久任务，包括已完成和中断记录。"""
        async with self.workspace.transaction(who, "work-task:one:" + task_id) as state:
            return require_task(state, task_id)

    async def find_request(self, who: Principal, request_id: str) -> WorkTaskRecord | None:
        """同请求重试优先读取已受理记录，不重新解析能力包或下载输入。"""
        async with self.workspace.transaction(who, "work-task:request") as state:
            return next((parse_record(value) for value in state.work_tasks.values()
                         if value["request_id"] == request_id), None)

    async def list(
        self, who: Principal, *, limit: int = 100, after_id: str | None = None
    ) -> builtins.list[WorkTaskRecord]:
        """按受理时间与标识稳定分页，普通员工与管理员都只有本人内容。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidRequestError("任务分页数量无效")
        async with self.workspace.transaction(who, "work-task:list") as state:
            values = sorted((parse_record(value) for value in state.work_tasks.values()),
                            key=lambda task: (-task.accepted_at, task.task_id))
            if after_id is not None:
                cursor = next((i for i, task in enumerate(values) if task.task_id == after_id), None)
                if cursor is None:
                    raise InvalidRequestError("任务分页游标无效")
                values = values[cursor + 1:]
            return values[:limit]

    async def events(
        self, who: Principal, task_id: str, *, after_id: int = 0, limit: int = 100
    ) -> builtins.list[WorkTaskEvent]:
        """补读本人持久事件，查询不会持有或启动执行任务。"""
        if type(after_id) is not int or not 0 <= after_id <= 9223372036854775807 or not 1 <= limit <= 100:
            raise InvalidRequestError("任务事件游标或数量无效")
        async with self.workspace.transaction(who, "work-task:events:" + task_id) as state:
            require_task(state, task_id)
            return [WorkTaskEvent.model_validate(entry) for entry in state.work_task_events
                    if entry["task_id"] == task_id and entry["event_id"] > after_id][:limit]

    async def transition(
        self, who: Principal, task_id: str, status: str, *, expected_version: int,
        at: float, event_kind: str = "state_changed", **changes: Any,
    ) -> WorkTaskRecord:
        """员工锁内CAS更新任务、持久事件和审计，异常时整体回滚。"""
        async with self.workspace.transaction(who, "work-task:one:" + task_id) as state:
            task = require_task(state, task_id)
            if task.state_version >= 256 and status in {"running", "waiting_user"}:
                raise ConflictError("任务状态更新次数已达上限，请人工核实")
            updated = advance(task, status, expected_version=expected_version, at=at, **changes)
            save_record(state, updated, event_kind)
            return updated

    async def lifecycle_task(self, tenant: str, user: str, task_id: str) -> WorkTaskRecord:
        """内部单项补领不装载员工全部任务。"""
        async with self.workspace.store.transaction(tenant, user, _lifecycle_guard,
                'work-task:one:' + task_id, allow_inactive=True) as state:
            return require_task(state, task_id)

    async def lifecycle_records(self, tenant: str, user: str, *, legacy_only: bool = False) -> builtins.list[WorkTaskRecord]:
        """内部收尾读取已停用员工的记录；不会赋予任何正文HTTP权限。"""
        async with self.workspace.store.transaction(tenant, user, _lifecycle_guard,
                'work-task:legacy-pending' if legacy_only else 'work-task:pending', allow_inactive=True) as state:
            return [parse_record(value) for value in state.work_tasks.values() if needs_recovery(value)
                and (not legacy_only or value.get('recovery_policy_version',1) == 1)]

    async def recover_artifact_index(self, task: WorkTaskRecord, receipts: tuple[ArtifactReceipt, ...],
                                     *, at: float) -> None:
        """中断后补齐已经存在的文件索引，不改变任务业务结论或创建行动项。"""
        async with self.workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                'work-task:lifecycle', allow_inactive=True) as state:
            current = require_task(state, task.task_id)
            if not current.artifact_recovery_pending:
                return
            updated = current.model_copy(update={'artifacts': receipts, 'artifact_recovery_pending': False,
                'outputs_verified': bool(receipts) and len(receipts) == len(current.artifact_intents),
                'state_version': current.state_version + 1, 'updated_at': max(at, current.updated_at)})
            save_record(state, updated, 'artifact_index_recovered')

    async def resources_closed(self, task: WorkTaskRecord, *, at: float) -> None:
        """独立登记资源已退出；终态业务结论保持原值，员工停用也可收尾。"""
        async with self.workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                'work-task:lifecycle', allow_inactive=True) as state:
            current = require_task(state, task.task_id)
            if task.recovery_policy_version == 2 and task.attempt_no != current.attempt_no:
                raise ConflictError('旧attempt不能清理新执行的资源责任')
            if current.resources_recovered and not current.resources_pending:
                return
            updated = current.model_copy(update={'resources_pending': False, 'resources_recovered': True,
                'state_version': current.state_version + 1, 'updated_at': max(at, current.updated_at)})
            save_record(state, updated, 'resources_closed')

    async def settle_lifecycle(
        self, tenant: str, user: str, task_id: str, status: str, *, at: float, error_code: str,
        expected_owner: str | None = None, expected_attempt: int | None = None, expected_epoch: int | None = None,
    ) -> WorkTaskRecord:
        """停用或进程关闭仍以同员工锁结算，只能进入终态，不发起新业务。"""
        from .models import TERMINAL

        if status not in TERMINAL - {'succeeded'}:
            raise ValueError('生命周期收尾只允许失败或中断终态')
        async with self.workspace.store.transaction(tenant, user, _lifecycle_guard,
                'work-task:lifecycle', allow_inactive=True) as state:
            current = require_task(state, task_id)
            if expected_epoch is not None:
                dispatch = state.task_dispatch.get(task_id)
                if dispatch is None or (dispatch['owner'],dispatch['epoch']) != (expected_owner,expected_epoch):
                    raise ConflictError('stale_epoch：旧租约不能结算后续执行')
            if (expected_owner is not None and (current.process_owner != expected_owner
                    or current.attempt_no != expected_attempt)):
                raise ConflictError('stale_epoch：旧attempt不能结算后续执行')
            if current.status in TERMINAL:
                return current
            target = 'outcome_unknown' if current.unverified_write else status
            updated = advance(current, target, expected_version=current.state_version,
                at=max(at, current.updated_at), error_code=error_code)
            save_record(state, updated, 'lifecycle_settled')
            return updated


async def _lifecycle_guard() -> None:
    """仅内部结算使用，身份已停用也不能阻止资源退出与中断回执。"""
