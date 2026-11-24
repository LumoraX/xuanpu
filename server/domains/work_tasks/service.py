"""员工任务受理：先核对输入，再持久提交，最后由进程托管执行。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.domains.work_tasks.models import (
    TERMINAL,
    CapabilityTaskInput,
    InputSnapshot,
    WorkTaskInput,
    WorkTaskRecord,
)

from .config import DurableExecutionConfig
from .rules import input_fingerprint
from .store import WorkTaskStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.core.tenancy import Principal
    from server.domains.capabilities.admission import CapabilityAdmission
    from server.domains.capabilities.resolver import CapabilityDecision
    from server.domains.connections.service import ConnectionService
    from server.domains.work_tasks.decisions import TaskDecision
    from server.domains.workspace.service import WorkspaceService
    from server.engine.packages import PackageRegistry

OFFICE_PACKAGE = "office-assistant"
MAX_INPUT_BYTES = 20 * 1024 * 1024
EXTENSIONS = {"text/plain": "txt", "text/markdown": "md", "text/csv": "csv",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx"}


class TaskExecutor(Protocol):
    """任务执行由服务级持有，HTTP请求不拥有它的生命周期。"""

    def start(self, who: Principal, task: WorkTaskRecord) -> None:
        """登记一个已持久受理任务的执行者，不等待模型完成。"""
        ...

    async def cancel(self, who: Principal, task: WorkTaskRecord) -> None:
        """请求停止并等待执行器核实结果，不伪造外部未执行。"""
        ...

    async def decide(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision) -> WorkTaskRecord:
        """复核并处理本人决定，由执行器消费后续事件。"""
        ...


@dataclass(frozen=True)
class CompletionProfile:
    """任务的执行方式与完成契约（R4-B2）：由能力修订决定，不来自客户端或模型。"""

    execution_kind: Literal['office', 'capability']
    completion_policy: Literal['office_files', 'capability_receipt']
    output_kind: Literal['table_summary', 'capability_result']


class WorkTaskService:
    """受理和人工决定与查询共用本人授权和同一持久状态。"""

    def __init__(
        self, workspace: WorkspaceService, packages: PackageRegistry, *, process_owner: str,
        now: Callable[[], float] = time.time, durable: DurableExecutionConfig | None = None,
    ) -> None:
        """注入本人工作区、版本化能力登记、进程身份和时钟。"""
        self.workspace, self.packages = workspace, packages
        self.store = WorkTaskStore(workspace)
        self.process_owner, self._now = process_owner, now
        self.connections: ConnectionService | None = None
        self.executor: TaskExecutor | None = None
        # 能力受理（R4-B1）：平台装配后注入；未注入时不做能力治理
        self.admission: CapabilityAdmission | None = None
        self.durable = durable or DurableExecutionConfig()
        self.store.durable_config = self.durable
        from server.engine.runtime.effect_reconciliation import EffectReconciliation

        self.reconciliation = EffectReconciliation(workspace, now=now)

    async def public_record(self, who: Principal, task: WorkTaskRecord) -> dict[str, Any]:
        """本人可见阶段及阻塞原因，内部执行凭据和私有对象键不外泄。"""
        from .store import require_task
        from .views import serialize

        async with self.workspace.transaction(who, 'work-task:view:' + task.task_id) as state:
            current=require_task(state,task.task_id)
            effects=[e for e in state.task_effects.values() if e['task_id']==task.task_id]
            result=serialize(current,state.task_dispatch.get(task.task_id),effects,set(self.reconciliation.adapters))
            result['task_token_limit']=self.durable.max_task_tokens if current.recovery_policy_version==2 else None
            result['activity_cursor']=max((e['event_id'] for e in state.work_task_events if e['task_id']==task.task_id),default=0)
        if current.pending:
            from .approval_views import approval_labels

            labels=await approval_labels(self.packages,current)
            for pending in result['pending']:pending.update(labels[pending['request_id']])
        return result

    async def accept(self, who: Principal, request: WorkTaskInput) -> WorkTaskRecord:
        """同请求先回放，新的交办核对全部输入再原子受理。"""
        tenant, user = await self.workspace.require_employee(who)
        fingerprint = input_fingerprint(request)
        previous = await self.store.find_request(who, request.request_id)
        if previous is not None:
            if previous.input_fingerprint != fingerprint:
                raise ConflictError("同一请求标识对应的交办内容不同")
            return previous
        decision = await self._admit(who, OFFICE_PACKAGE, None)
        package = await self.packages.resolve(
            tenant, OFFICE_PACKAGE, decision.package_version if decision else None)
        if self.durable.allows(tenant) and package.manifest.spec.policy.sandbox.network:
            raise InvalidRequestError('持久任务只允许断网脚本，外部操作请使用平台连接器')
        inputs = await self._inputs(who, request)
        table_mimes = {'text/csv','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}
        modes = {entry.mime_type in table_mimes for entry in inputs}
        if len(modes) != 1:
            raise InvalidRequestError('首期请将会议材料与表格材料分开交办')
        output_kind: Literal['table_summary','meeting_minutes'] = 'table_summary' if True in modes else 'meeting_minutes'
        now = self._now()
        record = WorkTaskRecord(task_id=new_id("task"), tenant_id=tenant, user_id=user, actor=who.actor,
            request_id=request.request_id, input_fingerprint=fingerprint, matter_id=request.matter_id,
            text=request.text, timezone=request.timezone, inputs=inputs,
            package=package.record.name, package_version=package.record.version,
            package_digest=package.record.digest.removeprefix("sha256:"),
            process_owner=self.process_owner, accepted_at=now, updated_at=now, output_kind=output_kind,
            recovery_policy_version=2 if self.durable.allows(tenant) else 1)
        accepted, created = await self.store.create_or_replay(who, record)
        await self._bind(who, accepted, decision, created)
        if created and self.executor is not None:
            self.executor.start(who, accepted)
        return accepted

    async def capability_profile(self, tenant: str, decision: CapabilityDecision | None,
                                 inputs: tuple[InputSnapshot, ...]) -> CompletionProfile:
        """完成契约（R4-B2）：只由已登记的能力修订决定，客户端与模型都不能指定。

        文件型能力（交付描述输出契约 ``office_files``）走办公任务的成果校验：固定 CSV 输入、
        ``outputs/manifest.json`` 与 table_summary 的四类成果，缺一即失败，不凭 HTTP 回执判成功。
        其余照旧是只读能力回执。
        """
        receipt = CompletionProfile('capability', 'capability_receipt', 'capability_result')
        if decision is None or self.admission is None:
            return receipt
        delivery = await self.admission.delivery(tenant, decision)
        if delivery.outputs.contract != 'office_files':
            return receipt
        if delivery.outputs.output_kind != 'table_summary':
            raise InvalidRequestError('该能力的成果类型暂不支持由能力交办')
        allowed = set(delivery.inputs.mime_types) & {'text/csv'}
        if (not allowed or not 1 <= len(inputs) <= delivery.inputs.max_files
                or any(entry.mime_type not in allowed for entry in inputs)):
            raise InvalidRequestError('该能力只接受 CSV 表格材料')
        return CompletionProfile('office', 'office_files', 'table_summary')

    async def _admit(self, who: Principal, package: str, entry: str | None) -> CapabilityDecision | None:
        """纳管包的新任务经能力决策（R4-B1）；未纳管或未装配能力治理时返回 None。"""
        if self.admission is None:
            return None
        return await self.admission.admit(who, package, entry, None)

    async def _bind(self, who: Principal, record: WorkTaskRecord, decision: CapabilityDecision | None,
                    created: bool) -> None:
        """新建的任务写调用绑定；进程若在建任务与写绑定之间退出，续跑时因缺绑定被拒（不猜依据）。"""
        if decision is not None and created and self.admission is not None:
            await self.admission.bind(who, "task", record.task_id, decision)

    async def accept_capability(self, who: Principal, request: CapabilityTaskInput) -> WorkTaskRecord:
        """目录项由实际发布包核对，不能上传脚本或指定宿主路径。"""
        tenant, user = await self.workspace.require_employee(who)
        if not self.durable.allows(tenant):
            raise ConflictError('持久能力交办尚未开启')
        fingerprint = input_fingerprint(request)
        previous = await self.store.find_request(who, request.request_id)
        if previous is not None:
            if previous.input_fingerprint != fingerprint:
                raise ConflictError('同一请求标识对应的交办内容不同')
            return previous
        decision = await self._admit(who, request.capability.package, request.capability.entry)
        package = await self.packages.resolve(
            tenant, request.capability.package, decision.package_version if decision else None)
        if request.capability.entry not in package.manifest.spec.skills.entries:
            raise InvalidRequestError('该能力入口尚未发布')
        if self.durable.allows(tenant) and package.manifest.spec.policy.sandbox.network:
            raise InvalidRequestError('持久任务只允许断网脚本，外部操作请使用平台连接器')
        inputs = await self._inputs(who, request)
        profile = await self.capability_profile(tenant, decision, inputs)
        now = self._now()
        record = WorkTaskRecord(task_id=new_id('task'), tenant_id=tenant, user_id=user, actor=who.actor,
            request_id=request.request_id, input_fingerprint=fingerprint, matter_id=request.matter_id,
            text=request.text, timezone=request.timezone, inputs=inputs, package=package.record.name,
            package_version=package.record.version, package_digest=package.record.digest.removeprefix('sha256:'),
            entry=request.capability.entry, recovery_policy_version=2, process_owner=self.process_owner,
            accepted_at=now, updated_at=now, execution_kind=profile.execution_kind,
            completion_policy=profile.completion_policy, output_kind=profile.output_kind)
        if self.connections is not None:
            from server.domains.connections.task_bindings import snapshot
            refs=await snapshot(self.connections,who,record.package_digest)
            record=record.model_copy(update={'connection_refs':refs})
        accepted, created = await self.store.create_or_replay(who, record)
        await self._bind(who, accepted, decision, created)
        if created and self.executor is not None:
            self.executor.start(who, accepted)
        return accepted

    async def _inputs(self, who: Principal, request: WorkTaskInput) -> tuple[InputSnapshot, ...]:
        """下载在事务外校验；提交时由存储层在同员工锁内再次核对。"""
        snapshots = []
        total = 0
        if len({entry.item_id for entry in request.input_files}) != len(request.input_files):
            raise InvalidRequestError("同一文件不能重复作为输入")
        for index, entry in enumerate(request.input_files, start=1):
            item = await self.workspace.get(who, entry.item_id)
            versions = await self.workspace.versions(who, entry.item_id)
            version = next((v for v in versions if v["revision"] == entry.revision), None)
            if item["trashed_at"] is not None or version is None:
                raise NotFoundError("输入文件正本或版本不存在")
            maximum = min(MAX_INPUT_BYTES, self.workspace.max_file_bytes)
            total += version["size"]
            if not 0 < version["size"] <= maximum or total > maximum:
                raise InvalidRequestError("输入文件为空或总大小超过20 MiB")
            content, mime, _ = await self.workspace.download(who, entry.item_id, entry.revision)
            if len(content) != version["size"] or hashlib.sha256(content).hexdigest() != version["sha256"]:
                raise ConflictError("输入文件正本校验失败")
            from .formats import validate_content

            validate_content(content, mime)
            extension = EXTENSIONS.get(mime)
            if extension is None:
                raise InvalidRequestError("暂不支持该输入格式，请提供文本、DOCX、PDF、CSV或XLSX正本")
            snapshots.append(InputSnapshot(item_id=entry.item_id, revision=entry.revision,
                sha256=version["sha256"], size=len(content), mime_type=mime,
                relative_name=f"input_{index}.{extension}"))
        return tuple(snapshots)

    async def cancel(self, who: Principal, task_id: str) -> WorkTaskRecord:
        """先持久登记停止意图，再通知执行者；已结束任务保持原结论。"""
        task = await self.store.get(who, task_id)
        if task.status in TERMINAL:
            return task
        if task.cancel_requested:
            if self.executor is not None:
                await self.executor.cancel(who,task)
            return await self.store.get(who,task_id)
        status = "cancelled" if task.status == "accepted" else task.status
        updated = await self.store.transition(who, task_id, status,
            expected_version=task.state_version, at=self._now(), cancel_requested=True,
            event_kind="cancel_requested")
        if self.executor is not None:
            await self.executor.cancel(who, updated)
        return await self.store.get(who, task_id)

    async def decide(self, who: Principal, task_id: str, decision: TaskDecision) -> WorkTaskRecord:
        """严格本人输入只作用于当前等待状态，不把展示文案当成回执。"""
        task = await self.store.get(who, task_id)
        if self.connections is not None and task.connection_refs:
            from server.core.errors import PermissionDeniedError
            from server.domains.connections.task_bindings import check_refs
            try:
                await check_refs(self.connections,who,task.connection_refs)
            except PermissionDeniedError as exc:
                raise ConflictError('connection_reauthorization_required') from exc
        if self.executor is None:
            raise ConflictError("任务执行器尚未准备好")
        return await self.executor.decide(who, task, decision)
