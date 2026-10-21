"""员工任务受理：先核对输入，再持久提交，最后由进程托管执行。"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING, Literal, Protocol

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.ids import new_id
from xuanpu_server.work_tasks.models import TERMINAL, InputSnapshot, WorkTaskInput, WorkTaskRecord

from .rules import input_fingerprint
from .store import WorkTaskStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.decisions import TaskDecision
    from xuanpu_server.workspace.service import WorkspaceService

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


class WorkTaskService:
    """受理和人工决定与查询共用本人授权和同一持久状态。"""

    def __init__(
        self, workspace: WorkspaceService, packages: PackageRegistry, *, process_owner: str,
        now: Callable[[], float] = time.time,
    ) -> None:
        """注入本人工作区、版本化能力登记、进程身份和时钟。"""
        self.workspace, self.packages = workspace, packages
        self.store = WorkTaskStore(workspace)
        self.process_owner, self._now = process_owner, now
        self.executor: TaskExecutor | None = None

    async def accept(self, who: Principal, request: WorkTaskInput) -> WorkTaskRecord:
        """同请求先回放，新的交办核对全部输入再原子受理。"""
        tenant, user = await self.workspace.require_employee(who)
        fingerprint = input_fingerprint(request)
        previous = await self.store.find_request(who, request.request_id)
        if previous is not None:
            if previous.input_fingerprint != fingerprint:
                raise ConflictError("同一请求标识对应的交办内容不同")
            return previous
        package = await self.packages.resolve(tenant, OFFICE_PACKAGE)
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
            process_owner=self.process_owner, accepted_at=now, updated_at=now, output_kind=output_kind)
        accepted, created = await self.store.create_or_replay(who, record)
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
        if task.status in TERMINAL or task.cancel_requested:
            return task
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
        if self.executor is None:
            raise ConflictError("任务执行器尚未准备好")
        return await self.executor.decide(who, task, decision)
