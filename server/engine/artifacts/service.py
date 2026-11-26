"""过程产物服务：不可变对象写入、预览、一键转存正式空间与跨节点只读接力挂载（ADR 0025）。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.core.objectstore import ObjectNotFoundError, validate_key
from server.engine.artifacts.store import TraceArtifactRecord, TraceArtifactStore, artifact_view
from server.engine.butler.pending import owner_of

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from server.core.objectstore import ObjectStore
    from server.core.tenancy import Principal
    from server.domains.workspace.service import WorkspaceService
    from server.engine.tracing.models import TraceOwner

MAX_ARTIFACT_BYTES = 20 * 1024 * 1024
PREVIEW_TEXT_LIMIT = 24_000


class TraceArtifactService:
    """管理挂在 ``trace_id`` 下的过程产物，隔离中间草稿与员工正式文件夹。"""

    def __init__(
        self,
        *,
        store: TraceArtifactStore,
        objects: ObjectStore,
        workspace: WorkspaceService,
        node_id: str,
        now: Callable[[], float] = time.time,
    ) -> None:
        """依赖由平台注入。"""
        self._store = store
        self._objects = objects
        self._workspace = workspace
        self._node_id = node_id
        self._now = now

    async def publish(
        self,
        owner: TraceOwner,
        *,
        trace_id: str,
        name: str,
        content: bytes,
        mime_type: str = "text/markdown",
        dispatch_id: str | None = None,
        step_index: int | None = None,
        contains_external_input: bool = False,
    ) -> TraceArtifactRecord:
        """将过程产物先写共享不可变 ObjectStore，再登记全局元数据索引（全节点可查）。"""
        if len(content) > MAX_ARTIFACT_BYTES:
            raise InvalidRequestError("过程产物超过 20 MiB 上限")
        artifact_id = new_id("art")
        digest = hashlib.sha256(content).hexdigest()
        object_key = validate_key(
            f"tenants/{owner.tenant_id}/artifacts/{trace_id}/{artifact_id}/{digest}"
        )
        await self._objects.put(object_key, content, content_type=mime_type)
        record = TraceArtifactRecord(
            artifact_id=artifact_id,
            tenant_id=owner.tenant_id,
            user_id=owner.user_id,
            trace_id=trace_id,
            dispatch_id=dispatch_id,
            step_index=step_index,
            name=name,
            mime_type=mime_type,
            size=len(content),
            sha256=digest,
            object_key=object_key,
            node_id=self._node_id,
            contains_external_input=contains_external_input,
            created_at=datetime.fromtimestamp(self._now(), UTC),
        )
        return await self._store.put(record)

    async def list_for_trace(self, who: Principal, trace_id: str) -> tuple[TraceArtifactRecord, ...]:
        """列出本人指定 trace 下的过程产物。"""
        await self._workspace.require_employee(who)
        return await self._store.list_for_trace(owner_of(who), trace_id)

    async def read_bytes(
        self, owner: TraceOwner, artifact_id: str,
    ) -> tuple[bytes, TraceArtifactRecord]:
        """读取产物正本并强制核验 SHA-256 完整性。"""
        record = await self._store.get(owner, artifact_id)
        try:
            content = await self._objects.get_limited(record.object_key, max_bytes=MAX_ARTIFACT_BYTES)
        except ObjectNotFoundError as exc:
            raise NotFoundError("过程产物对象不存在") from exc
        if hashlib.sha256(content).hexdigest() != record.sha256:
            raise ConflictError("过程产物摘要校验失败")
        return content, record

    async def preview(self, who: Principal, artifact_id: str) -> dict[str, Any]:
        """纯 Web 产物预览接口：文本/Markdown/JSON/CSV 返回有界 UTF-8 正文，其余仅回元数据。"""
        await self._workspace.require_employee(who)
        content, record = await self.read_bytes(owner_of(who), artifact_id)
        text: str | None = None
        truncated = False
        if record.mime_type.startswith("text/") or record.mime_type in (
            "application/json", "image/svg+xml",
        ):
            decoded = content.decode("utf-8", errors="replace")
            if len(decoded) > PREVIEW_TEXT_LIMIT:
                text = decoded[:PREVIEW_TEXT_LIMIT]
                truncated = True
            else:
                text = decoded
        return {**artifact_view(record), "text": text, "truncated": truncated}

    async def promote_to_workspace(
        self,
        who: Principal,
        artifact_id: str,
        *,
        request_id: str,
        parent_id: str | None = None,
        matter_id: str | None = None,
    ) -> dict[str, Any]:
        """将过程产物一键转存进员工个人正式工作台，并回填关联文件 ID。"""
        await self._workspace.require_employee(who)
        owner = owner_of(who)
        content, record = await self.read_bytes(owner, artifact_id)
        saved = await self._workspace.upload(
            who, record.name, content, record.mime_type, request_id,
            parent_id=parent_id, matter_id=matter_id,
        )
        updated = await self._store.mark_promoted(owner, artifact_id, str(saved["id"]))
        return {"artifact": artifact_view(updated), "file": saved}

    async def materialize_readonly_inputs(
        self, owner: TraceOwner, artifact_ids: Sequence[str], dest_dir: Path,
    ) -> tuple[tuple[Path, ...], bool]:
        """跨节点接力挂载：从共享 ObjectStore 下载核验后以 0444 只读落入目标沙盒输入目录，并传播外部输入污点。"""
        await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)
        written: list[Path] = []
        tainted = False
        for idx, artifact_id in enumerate(artifact_ids, start=1):
            content, record = await self.read_bytes(owner, artifact_id)
            tainted = tainted or record.contains_external_input
            target = dest_dir / f"{idx:02d}_{record.name}"

            def _write_ro(path: Path = target, raw: bytes = content) -> None:
                path.write_bytes(raw)
                os.chmod(path, 0o444)

            await asyncio.to_thread(_write_ro)
            written.append(target)
        return tuple(written), tainted
