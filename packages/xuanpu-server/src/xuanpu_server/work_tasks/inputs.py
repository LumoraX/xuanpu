"""在启动执行前，按固定版本形成独立的只读挂载来源。"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.package_cache import run_to_completion
from xuanpu_server.runtime.execution_scope import ExecutionScope

from .formats import validate_content
from .service import MAX_INPUT_BYTES

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.work_tasks.models import WorkTaskRecord
    from xuanpu_server.workspace.service import WorkspaceService


class InputSnapshotService:
    """来源始终经本人工作区读取，快照根不在任务可写空间内。"""

    def __init__(self, workspace: WorkspaceService, root: Path) -> None:
        """注入工作区授权服务和平台专属快照目录。"""
        self.workspace, self.root = workspace, root

    async def stage(self, who: Principal, task: WorkTaskRecord) -> Path:
        """重验归属、固定版本及摘要后一次写入，不挂载整个个人空间。"""
        tenant, user = await self.workspace.require_employee(who)
        if (tenant, user) != (task.tenant_id, task.user_id):
            raise NotFoundError('工作任务不存在')
        if not 1 <= len(task.inputs) <= 8:
            raise InvalidRequestError('输入数量须为1至8个')
        contents: list[tuple[str, bytes]] = []
        total = 0
        for entry in task.inputs:
            item = await self.workspace.get(who, entry.item_id)
            if item['trashed_at'] is not None:
                raise NotFoundError('输入文件已回收')
            content, mime, _ = await self.workspace.download(who, entry.item_id, entry.revision)
            total += len(content)
            if total > min(MAX_INPUT_BYTES, self.workspace.max_file_bytes):
                raise InvalidRequestError('输入总量超限')
            if (len(content) != entry.size or mime != entry.mime_type
                or hashlib.sha256(content).hexdigest() != entry.sha256):
                raise ConflictError('固定输入内容校验失败')
            validate_content(content, mime)
            contents.append((entry.relative_name, content))
        created: list[Path] = []
        try:
            return await run_to_completion(self._write, contents, created, self.directory(task))
        except BaseException:
            for directory in created:
                await run_to_completion(shutil.rmtree, directory)
            raise

    def _write(self, contents: list[tuple[str, bytes]], created: list[Path], owner_root: Path) -> Path:
        """私有目录写完才交付；异常清理半成品，调用取消也等待线程结束。"""
        owner_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(tempfile.mkdtemp(prefix='input-', dir=owner_root))
        created.append(directory)
        try:
            for name, content in contents:
                if Path(name).name != name or name in {'.', '..'}:
                    raise InvalidRequestError('输入相对名称不合法')
                with (directory / name).open('xb') as handle:
                    handle.write(content)
            return directory
        except BaseException:
            shutil.rmtree(directory)
            created.clear()
            raise

    def directory(self, task: WorkTaskRecord) -> Path:
        """任务专属输入根可以在重启后确定，不包含客户端提供的路径。"""
        scope = ExecutionScope(task.tenant_id, task.user_id, 'task:' + task.task_id)
        return self.root / scope.storage_key()

    async def cleanup(self, task: WorkTaskRecord, *, keep_root: bool = False) -> None:
        """调用者已停止该任务所有执行资源后，删除它的固定输入副本。"""
        directory = self.directory(task)
        if await run_to_completion(directory.exists):
            if keep_root:
                await run_to_completion(self._empty, directory)
            else:
                await run_to_completion(shutil.rmtree, directory)

    @staticmethod
    def _empty(directory: Path) -> None:
        """挂起任务仅保留空根inode，避免远端文件共享仍缓存已删除的挂载父目录。"""
        for child in directory.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
