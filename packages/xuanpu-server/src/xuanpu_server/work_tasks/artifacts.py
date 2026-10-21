"""在执行完全冻结后校验候选文件，并复用个人文件幂等回执保存成果。"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from taifeng_sandbox import SandboxPolicyError, export_files

from xuanpu_server.errors import ConflictError, InvalidRequestError

from .artifact_rules import OutputManifest, json_value, manifest_from
from .artifact_staging import ArtifactStaging
from .formats import validate_content
from .models import ArtifactIntent, ArtifactReceipt, InputFile

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.runtime.sandbox import SandboxHandle
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.models import WorkTaskRecord
    from xuanpu_server.workspace.service import WorkspaceService

    from .store import WorkTaskStore


class ArtifactService:
    """模型完成与文件保存分离；每项上传成功后才写任务成果索引。"""

    def __init__(self, workspace: WorkspaceService, store: WorkTaskStore, *,
                 now: Callable[[], float] = time.time) -> None:
        """注入相同员工工作区事务和时钟。"""
        self.workspace, self.store, self.now = workspace, store, now
        self.staging = ArtifactStaging(workspace, store, now)

    async def commit(self, who: Principal, task: WorkTaskRecord, handle: SandboxHandle,
                     prepare: Callable[[list[dict[str, Any]]], Any] | None = None
                     ) -> tuple[tuple[ArtifactReceipt, ...], list[dict[str, Any]]]:
        """冻结实际容器全部进程，从明确绑定根导出；远端后端不得隐式兜底。"""
        await self.workspace.require_employee(who)
        from .execution_context import current_execution

        binding = current_execution.get()
        if binding is not None:
            await binding.store.checkpoint(binding.lease, 'freezing_outputs', now=self.now())
        await handle.freeze_outputs()
        try:
            raw = await export_files(handle.workspace, ['outputs/manifest.json'], max_total_bytes=65536)
            manifest = manifest_from(raw['outputs/manifest.json'].content)
            current = await self.store.get(who, task.task_id)
            if manifest.kind != task.output_kind:
                raise InvalidRequestError('成果类型与固定输入不匹配')
            if manifest.kind == 'table_summary' and (
                current.output_hashes.get('outputs/manifest.json') != raw['outputs/manifest.json'].sha256
            ):
                raise InvalidRequestError('缺少真实汇总脚本回执，或成果在脚本执行后被改写')
            files = await export_files(handle.workspace, [entry.path for entry in manifest.artifacts])
        except SandboxPolicyError as exc:
            raise InvalidRequestError('成果文件缺失、不安全或超过资源限制') from exc
        actions = self._validate_files(task, current, manifest, files)
        current = await self.store.get(who, task.task_id)
        if current.cancel_requested or current.status != 'running':
            raise ConflictError('任务当前不能保存成果')
        if current.outputs_verified:
            return current.artifacts, actions
        intents = tuple(ArtifactIntent(name=entry.name, role=entry.role, mime_type=entry.mime_type,
            size=files[entry.path].size, sha256=files[entry.path].sha256) for entry in manifest.artifacts)
        if task.recovery_policy_version == 2:
            if prepare is not None:
                await prepare(actions)
            await self.staging.stage(who, task, intents, [files[entry.path].content for entry in manifest.artifacts])
            return await self.staging.commit(who, task), actions
        await self.store.transition(who, task.task_id, 'running', expected_version=current.state_version,
            at=self.now(), event_kind='artifact_uploads_started' , artifact_intents=intents,
            artifact_recovery_pending=True)
        folder = await self.workspace.create(who, "folder", {"name": "办公成果-" + task.task_id},
                                             f"artifact:{task.task_id}:folder")
        receipts = []
        references = tuple(InputFile(item_id=entry.item_id, revision=entry.revision) for entry in task.inputs)
        for index, entry in enumerate(manifest.artifacts):
            frozen = files[entry.path]
            item = await self.workspace.upload(who, entry.name, frozen.content, entry.mime_type,
                                               f'artifact:{task.task_id}:{index}', parent_id=folder['id'],
                                               matter_id=task.matter_id)
            receipts.append(ArtifactReceipt(item_id=item['id'], revision=item['data']['current_revision'],
                name=entry.name, role=entry.role, sha256=frozen.sha256, size=frozen.size,
                mime_type=entry.mime_type, input_refs=references, generated_at=self.now()))
        current = await self.store.get(who, task.task_id)
        updated = await self.store.transition(who, task.task_id, 'running',
            expected_version=current.state_version, at=self.now(), event_kind='artifacts_saved',
            artifacts=tuple(receipts), outputs_verified=True, artifact_recovery_pending=False)
        return updated.artifacts, actions

    def _validate_files(self, task: WorkTaskRecord, current: WorkTaskRecord,
                        manifest: OutputManifest, files: dict[str, Any]) -> list[dict[str, Any]]:
        """逐份验证格式及实际脚本摘要，校验完成前不建立上传意图。"""
        actions: list[dict[str, Any]] = []
        for entry in manifest.artifacts:
            data = files[entry.path].content
            if manifest.kind == 'table_summary' and current.output_hashes.get(entry.path) != files[entry.path].sha256:
                raise InvalidRequestError('表格成果与实际脚本执行回执不符')
            if entry.mime_type == 'application/json':
                actions.extend(self._json_artifact(task, entry.role, data))
            else:
                validate_content(data, entry.mime_type)
        return actions

    def _json_artifact(self, task: WorkTaskRecord, role: str, data: bytes) -> list[dict[str, Any]]:
        """验证证据与候选结构，不将候选内容直接写成原生记录。"""
        value = json_value(data)
        if role == 'evidence':
            self._evidence(task, value)
        if role == 'actions':
            if (not isinstance(value, list) or len(value) > 32
                or any(not isinstance(action, dict) for action in value)):
                raise InvalidRequestError('行动项成果须为最多32项的候选数组')
            return value
        return []

    async def recover_index(self, task: WorkTaskRecord) -> None:
        """只找已提交上传回执并核对实际对象，不创建文件，也不重跑任何执行。"""
        import hashlib

        from .store import _lifecycle_guard

        receipts = []
        references = tuple(InputFile(item_id=entry.item_id, revision=entry.revision) for entry in task.inputs)
        for index, intent in enumerate(task.artifact_intents):
            async with self.workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                    f'artifact:{task.task_id}:{index}', allow_inactive=True) as state:
                saved = state.requests.get(f'artifact:{task.task_id}:{index}')
                if saved is None:
                    continue
                item_id = saved['result']['id']
                revision = saved['result']['data']['current_revision']
                version = next(entry for entry in state.versions[item_id] if entry['revision'] == revision)
                if any(version[key] != getattr(intent, key) for key in ('sha256','size','mime_type')):
                    raise ConflictError('已上传成果与冻结清单不一致，请人工核实')
                object_key, generated = version['object_key'], version['created_at']
            content = await self.workspace.objects.get_limited(object_key, max_bytes=intent.size)
            if len(content) != intent.size or hashlib.sha256(content).hexdigest() != intent.sha256:
                raise ConflictError('已上传成果的对象摘要不一致，请人工核实')
            receipts.append(ArtifactReceipt(**intent.model_dump(), item_id=item_id, revision=revision,
                input_refs=references, generated_at=generated))
        await self.store.recover_artifact_index(task, tuple(receipts), at=self.now())

    @staticmethod
    def _evidence(task: WorkTaskRecord, value: Any) -> None:
        """脚本来源清单必须覆盖固定输入，记录数量和行定位必须完整。"""
        expected = {entry.relative_name:entry.sha256 for entry in task.inputs}
        try:
            actual = {entry['name']:entry['sha256'] for entry in value['inputs']}
            rows = value['rows']
            valid = (value['schema_version'] == 1 and actual == expected
                and len(value['inputs']) == len(expected) and value['record_count'] == len(rows)
                and 0 < len(rows) <= 10000 and all(row['file'] in expected
                    and isinstance(row['sheet'],str) and type(row['row']) is int and row['row'] >= 2
                    for row in rows))
        except (TypeError, KeyError) as exc:
            raise InvalidRequestError('表格行证据结构不完整') from exc
        if not valid:
            raise InvalidRequestError('表格行证据与固定来源不一致')
