"""成果冻结后保存私有字节，并从完整检查点幂等补传。"""
from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.execution_context import current_execution
from xuanpu_server.work_tasks.models import ArtifactReceipt, InputFile, StagedArtifact

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.models import ArtifactIntent, WorkTaskRecord
    from xuanpu_server.work_tasks.store import WorkTaskStore
    from xuanpu_server.workspace.service import WorkspaceService


class ArtifactStaging:
    """对象意图先于put，完整核验后才允许无模型恢复上传。"""

    def __init__(self, workspace: WorkspaceService, store: WorkTaskStore, now: Callable[[], float]) -> None:
        """复用平台对象存储；私有前缀不挂载给执行器。"""
        self.workspace, self.store, self.now = workspace, store, now

    async def stage(self, who: Principal, task: WorkTaskRecord, intents: tuple[ArtifactIntent, ...],
                    contents: list[bytes]) -> None:
        """先登记明确key及摘要，put结果未知也不会丢失清理责任。"""
        binding = current_execution.get()
        if binding is None or binding.lease.task_id != task.task_id:
            raise ConflictError('暂存必须属于有效attempt')
        current = await self.store.get(who, task.task_id)
        objects = tuple(StagedArtifact(**intent.model_dump(), attempt_no=task.attempt_no,
            object_key=f'{task.tenant_id}/task-staging/{task.task_id}/{task.attempt_no}/{index}-{intent.sha256}',
            created_at=self.now()) for index, intent in enumerate(intents))
        await self.store.transition(who, task.task_id, 'running', expected_version=current.state_version,
            at=self.now(), event_kind='staging_intents', staging_objects=(*current.staging_objects, *objects),
            artifact_intents=intents, artifact_recovery_pending=True)
        for entry, data in zip(objects, contents, strict=True):
            await binding.store.checkpoint(binding.lease, 'artifact_staging', {'key_digest':
                hashlib.sha256(entry.object_key.encode()).hexdigest()}, now=self.now())
            await self.workspace.objects.put(entry.object_key, data, content_type=entry.mime_type)
            await self._read(entry)
        current = await self.store.get(who, task.task_id)
        keys = {entry.object_key for entry in objects}
        verified = tuple(entry.model_copy(update={'verified': True}) if entry.object_key in keys else entry
                         for entry in current.staging_objects)
        await binding.store.checkpoint(binding.lease, 'artifacts_staged',
            {'validator_version': 1, 'input_digests': [entry.sha256 for entry in task.inputs],
             'artifacts': [entry.model_dump(mode='json', exclude={'object_key'}) for entry in objects]},
            now=self.now(), task_changes={'staging_objects': verified,
                'frozen_staging': tuple(entry.object_key for entry in objects)})

    async def _read(self, entry: StagedArtifact) -> bytes:
        """恢复与首次保存都核对实际字节，不信任对象存在或清单自报。"""
        data = await self.workspace.objects.get_limited(entry.object_key, max_bytes=entry.size)
        digest = await asyncio.to_thread(lambda: hashlib.sha256(data).hexdigest())
        if len(data) != entry.size or digest != entry.sha256:
            raise ConflictError('暂存成果摘要不匹配')
        return data

    async def commit(self, who: Principal, task: WorkTaskRecord) -> tuple[ArtifactReceipt, ...]:
        """完整读取暂存检查点，部分上传复用原请求回执且不重新采样。"""
        current = await self.store.get(who, task.task_id)
        entries = {entry.object_key: entry for entry in current.staging_objects}
        if not current.frozen_staging or any(key not in entries for key in current.frozen_staging):
            raise ConflictError('没有完整冻结检查点')
        objects = [entries[key] for key in current.frozen_staging]
        if len(objects) != len(current.artifact_intents) or any(not entry.verified for entry in objects):
            raise ConflictError('冻结检查点尚未完整核验')
        # 全部对象先核验，不能在发现后半份篡改前把前半份宣告成功。
        contents = [await self._read(entry) for entry in objects]
        folder = await self.workspace.create(who, 'folder', {'name': '办公成果-' + task.task_id},
                                             f'artifact:{task.task_id}:folder')
        references = tuple(InputFile(item_id=entry.item_id, revision=entry.revision) for entry in task.inputs)
        receipts = []
        for index, (entry, data) in enumerate(zip(objects, contents, strict=True)):
            item = await self.workspace.upload(who, entry.name, data, entry.mime_type,
                f'artifact:{task.task_id}:{index}', parent_id=folder['id'], matter_id=task.matter_id)
            downloaded, mime, _ = await self.workspace.download(who, item['id'], item['data']['current_revision'])
            if mime != entry.mime_type or hashlib.sha256(downloaded).hexdigest() != entry.sha256:
                raise ConflictError('已有上传回执与冻结成果不一致')
            receipts.append(ArtifactReceipt(**entry.model_dump(exclude={
                'object_key', 'attempt_no', 'created_at', 'verified', 'deleted'}), item_id=item['id'],
                revision=item['data']['current_revision'], input_refs=references, generated_at=self.now()))
            binding = current_execution.get()
            if binding is not None:
                # 保留完整冻结阶段，同时追加可观察的单项上传进展。
                await binding.store.checkpoint(binding.lease, 'artifacts_staged',
                    {'validator_version': 1, 'uploaded': index + 1}, now=self.now())
        current = await self.store.get(who, task.task_id)
        await self.store.transition(who, task.task_id, 'running', expected_version=current.state_version,
            at=self.now(), event_kind='artifacts_saved', artifacts=tuple(receipts),
            outputs_verified=True, artifact_recovery_pending=False)
        return tuple(receipts)

    async def collect(self, task: WorkTaskRecord) -> int:
        """只删除终态超过24小时且无资源责任的已知私有对象。"""
        from .models import TERMINAL
        from .store import _lifecycle_guard, require_task, save_record

        if task.status not in TERMINAL or task.resources_pending or task.artifact_recovery_pending:
            return 0
        count = 0
        for entry in task.staging_objects:
            if entry.deleted or self.now() - entry.created_at < 86400:
                continue
            await self.workspace.objects.delete(entry.object_key)
            async with self.workspace.store.transaction(task.tenant_id, task.user_id, _lifecycle_guard,
                    'work-task:one:' + task.task_id, allow_inactive=True) as state:
                current = require_task(state, task.task_id)
                if current.status not in TERMINAL or current.resources_pending or current.artifact_recovery_pending:
                    raise ConflictError('成果读取或清理责任发生变化')
                updated = current.model_copy(update={'staging_objects': tuple(
                    obj.model_copy(update={'deleted': True}) if obj.object_key == entry.object_key else obj
                    for obj in current.staging_objects), 'state_version': current.state_version + 1,
                    'updated_at': max(self.now(), current.updated_at)})
                save_record(state, updated, 'staging_collected')
            count += 1
        return count
