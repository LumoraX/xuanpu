"""独立后台处理：原件加密先行、逐项检查点、过期租约 fencing。"""

from __future__ import annotations

import asyncio
import hashlib
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol

import anyio
from anyio import to_process

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.parser import parse_invoice
from xuanpu_finance.models import FileRef, FinancePrincipal
from xuanpu_finance.storage.common import decode

if TYPE_CHECKING:
    from xuanpu_finance.objects import OriginalObjects
    from xuanpu_finance.storage import FinanceStore


def _lease_lost(error: BaseException) -> bool:
    """只将明确的租约失效视为正常退让，其他错误必须传播。"""
    if isinstance(error, BaseExceptionGroup):
        return all(_lease_lost(child) for child in error.exceptions)
    return isinstance(error, FinanceError) and error.code == "LEASE_LOST"


def _needs_original(data: bytes, content_type: str, filename: str) -> bool:
    """识别无需执行内容的 PDF/OFD/图片封装；XML 不按文件名猜 profile。"""
    header = data.lstrip()
    signatures = (
        b"%PDF",
        b"\x89PNG\r\n\x1a\n",
        b"\xff\xd8\xff",
        b"GIF87a",
        b"GIF89a",
        b"II*\x00",
        b"MM\x00*",
        b"BM",
    )
    if header.startswith(signatures):
        return True
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return True
    return header.startswith(b"PK\x03\x04") and (
        filename.lower().endswith(".ofd")
        or content_type.lower() in {"application/ofd", "application/vnd.ofd"}
    )


class FilesGateway(Protocol):
    """通过平台受控出口检查状态并取文件。"""

    async def active(self, who: FinancePrincipal) -> bool:
        """查询用户当前活跃状态。"""
        ...

    async def download(self, who: FinancePrincipal, file: FileRef) -> tuple[bytes, str, str]:
        """返回原字节、内容类型与文件名。"""
        ...


class FinanceWorker:
    """不依赖平台运行时的持久导入处理器。"""

    def __init__(
        self,
        store: FinanceStore,
        objects: OriginalObjects,
        gateway: FilesGateway,
        synthetic: bool = False,
    ) -> None:
        """显式注入持久存储、文件出口和测试 profile 开关。"""
        self.store, self.objects, self.gateway, self.synthetic = store, objects, gateway, synthetic

    async def _heartbeat(self, claim: dict[str, Any]) -> None:
        """处理大批次时续租，失败触发任务组取消以停止旧 worker。"""
        while True:
            await asyncio.sleep(20)
            await self.store.renew_import(claim)

    async def run_once(self) -> bool:
        """持久认领一次；数据库故障向上传播，重启从已提交检查点恢复。"""
        claim = await self.store.claim_import()
        if claim is None:
            return False
        try:
            async with asyncio.TaskGroup() as group:
                heartbeat = group.create_task(self._heartbeat(claim))
                try:
                    for item in await self.store.pending_items(claim):
                        await self._process(claim, item)
                    await self.store.complete_import(claim)
                finally:
                    heartbeat.cancel()
        except BaseExceptionGroup as exc:
            if not _lease_lost(exc):
                raise
        finally:
            await self.store.release_import(claim)
        return True

    async def _process(self, claim: dict[str, Any], item: dict[str, Any]) -> None:
        """只捕获外部依赖及可解释领域错误，不吞数据库失败。"""
        who = FinancePrincipal(claim["tenant_id"], claim["owner_id"])
        try:
            await self.store.ensure_worker_access(claim)
        except FinanceError as exc:
            await self.store.finish_item_error(claim, item["position"], "failed", exc.code)
            return
        try:
            active = await self.gateway.active(who)
        except Exception:
            await self.store.finish_item_error(
                claim, item["position"], "failed", "IDENTITY_GATEWAY_FAILED"
            )
            return
        if not active:
            await self.store.finish_item_error(
                claim, item["position"], "failed", "ACCOUNT_INACTIVE"
            )
            return
        if item["state"] == "pending":
            received = await self._receive(claim, item, who)
            if received is None:
                return
            item = received
        await self._parse(claim, item)

    async def _receive(
        self, claim: dict[str, Any], item: dict[str, Any], who: FinancePrincipal
    ) -> dict[str, Any] | None:
        """先恢复已预登记的副本，再下载并在对象写前持久责任索引。"""
        if item["object_key"] is not None:
            recovered, result = await self._recover_original(claim, item)
            if recovered:
                return result
        file = FileRef.model_validate(decode(item["file_ref"]))
        try:
            data, content_type, filename = await self.gateway.download(who, file)
        except Exception:
            await self.store.finish_item_error(
                claim, item["position"], "failed", "FILE_DOWNLOAD_FAILED"
            )
            return None
        if len(data) > 20 * 1024 * 1024:
            await self.store.finish_item_error(
                claim, item["position"], "quarantined", "FILE_TOO_LARGE"
            )
            return None
        try:
            item = await self.store.prepare_original(
                claim,
                item["position"],
                hashlib.sha256(data).hexdigest(),
                len(data),
                content_type,
                filename,
            )
        except FinanceError as exc:
            if exc.code not in {"IMPORT_SIZE_EXCEEDED", "ORIGINAL_INDEX_CONFLICT"}:
                raise
            await self.store.finish_item_error(claim, item["position"], "quarantined", exc.code)
            return None
        try:
            await self.objects.put(item["object_key"], data)
        except Exception:
            await self.store.finish_item_error(
                claim, item["position"], "failed", "OBJECT_STORAGE_FAILED"
            )
            return None
        return await self._record_original(claim, item)

    async def _recover_original(
        self, claim: dict[str, Any], item: dict[str, Any]
    ) -> tuple[bool, dict[str, Any] | None]:
        """已写对象优先核对责任摘要，无须重新访问已删除的平台源文件。"""
        try:
            data = await self.objects.get(item["object_key"])
        except FinanceError as exc:
            if exc.code == "OBJECT_NOT_FOUND":
                return False, None
            await self.store.finish_item_error(
                claim, item["position"], "failed", "OBJECT_STORAGE_FAILED"
            )
            return True, None
        except Exception:
            await self.store.finish_item_error(
                claim, item["position"], "failed", "OBJECT_STORAGE_FAILED"
            )
            return True, None
        if (
            len(data) != item["size_bytes"]
            or hashlib.sha256(data).hexdigest() != item["actual_sha256"]
        ):
            await self.store.finish_item_error(
                claim, item["position"], "quarantined", "OBJECT_HASH_MISMATCH"
            )
            return True, None
        return True, await self._record_original(claim, item)

    async def _record_original(self, claim: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        """只使用已持久的责任元数据重建回执，数据库故障向上传播。"""
        return await self.store.receive_original(
            claim,
            item["position"],
            item["object_key"],
            item["actual_sha256"],
            item["size_bytes"],
            item["content_type"],
            item["filename"],
        )

    async def _parse(self, claim: dict[str, Any], item: dict[str, Any]) -> None:
        """未知 XML profile 隔离，PDF/OFD/图片保留原件后等待可解析原件。"""
        file = FileRef.model_validate(decode(item["file_ref"]))
        if item["actual_sha256"] != file.sha256:
            await self.store.finish_item_error(
                claim, item["position"], "quarantined", "FILE_HASH_MISMATCH"
            )
            return
        try:
            data = await self.objects.get(item["object_key"])
        except Exception:
            await self.store.finish_item_error(
                claim, item["position"], "failed", "OBJECT_STORAGE_FAILED"
            )
            return
        if hashlib.sha256(data).hexdigest() != item["actual_sha256"]:
            await self.store.finish_item_error(
                claim, item["position"], "quarantined", "OBJECT_HASH_MISMATCH"
            )
            return
        if _needs_original(data, item["content_type"], item["filename"]):
            await self.store.finish_item_error(
                claim, item["position"], "awaiting_original", "ORIGINAL_REQUIRED"
            )
            return
        try:
            with anyio.fail_after(10):
                parsed = await to_process.run_sync(
                    partial(parse_invoice, data, synthetic=self.synthetic), cancellable=True
                )
        except TimeoutError:
            await self.store.finish_item_error(
                claim, item["position"], "quarantined", "PARSE_TIMEOUT"
            )
            return
        except FinanceError as exc:
            await self.store.finish_item_error(claim, item["position"], "quarantined", exc.code)
            return
        try:
            await self.store.finish_item_parsed(claim, item["position"], parsed)
        except FinanceError as exc:
            if exc.code not in {
                "DUPLICATE_CONTENT_CONFLICT",
                "DUPLICATE_RESTRICTED",
                "SUPPLEMENT_CONFLICT",
            }:
                raise
            await self.store.finish_item_error(claim, item["position"], "quarantined", exc.code)
