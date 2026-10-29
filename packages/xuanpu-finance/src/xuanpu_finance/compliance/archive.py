"""归档物化、固定包上传和全量版本锁读回管线。"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_finance.compliance.archive_checks import (
    canonical,
    materialize,
    reports,
    validate_profile,
)
from xuanpu_finance.compliance.archive_http import ArchiveHTTP
from xuanpu_finance.compliance.archive_models import ArchiveSnapshot
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.provider import ProviderResponseError, _unique_object

if TYPE_CHECKING:
    from xuanpu_finance.compliance.evidence import EvidenceFetcher
    from xuanpu_finance.compliance.provider import ComplianceRegistry
    from xuanpu_finance.objects import OriginalObjects


def _zip(members: dict[str, bytes]) -> bytes:
    """固定时间和顺序生成ZIP，重启物化不会更改原包。"""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, raw in sorted(members.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            archive.writestr(entry, raw)
    return stream.getvalue()


class ArchivePipeline:
    """事务外物化，事务内发送准备由独立Store再次复核。"""

    def __init__(self, registry: ComplianceRegistry, objects: OriginalObjects,
                 fetcher: EvidenceFetcher | None = None) -> None:
        """所有资源显式注入，不借平台数据库或宿主云凭据。"""
        self.registry, self.objects, self.fetcher = registry, objects, fetcher
        self.http = ArchiveHTTP(registry)

    def _key(self, request: dict[str, Any], suffix: str) -> str:
        """按冻结请求摘要隔离物化对象，不接受外部指定缓存路径。"""
        return "f3/archive-staging/" + str(request["payload_hash"]) + "/" + suffix

    async def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        """实际读取每项材料并生成确定性清册和加密物化包。"""
        self.registry._request_config(request, submit=True)
        try:
            snapshot = ArchiveSnapshot.model_validate(request["payload"]["archive_snapshot"])
            validate_profile(snapshot, request)
        except (ValidationError, KeyError, ValueError) as error:
            raise FinanceError("ARCHIVE_SNAPSHOT_INVALID", 409, "档案输入缺件或格式无效") from error
        try:
            cached = await self._descriptor(request)
        except FinanceError as error:
            if error.code != "OBJECT_NOT_FOUND":
                raise
        else:
            bundle = await self.objects.get(cached["bundle_key"])
            if hashlib.sha256(bundle).hexdigest() != cached["bundle_sha256"]:
                raise FinanceError("ARCHIVE_STAGING_CORRUPT", 409, "固定物化包摘要不符")
            return cached
        checked_at = await self._checked_at(request)
        members: dict[str, bytes] = {}
        for artifact in snapshot.artifacts:
            raw = await materialize(artifact, self.objects, self.fetcher)
            members[artifact.path] = raw
            await self.objects.put(self._key(request, "material-" + artifact.sha256), raw)
        qualities = reports(snapshot, checked_at)
        manifest = dict(protocol_version="1", simulation=True, target=snapshot.target,
            content_version=snapshot.content_version, compliance_version=snapshot.compliance_version,
            event_cutoff=snapshot.event_cutoff, snapshots=snapshot.snapshots,
            source_snapshot_hash=snapshot.source_snapshot_hash, profile=snapshot.profile.model_dump(),
            retention=snapshot.retention.model_dump(), parent_manifest_hash=snapshot.parent_manifest_hash,
            required_roles=snapshot.required_roles, four_qualities=qualities,
            members=[a.model_dump(exclude={"source"}) for a in snapshot.artifacts])
        manifest_raw = canonical(manifest)
        members["manifest.json"] = manifest_raw
        bundle = await asyncio.to_thread(_zip, members)
        if len(bundle) > 100*1024*1024:
            raise FinanceError("ARCHIVE_BUNDLE_TOO_LARGE", 413, "归档包超过100MiB")
        descriptor: dict[str, Any] = dict(bundle_key=self._key(request, "bundle.zip"),
            bundle_sha256=hashlib.sha256(bundle).hexdigest(), manifest=manifest,
            manifest_hash=hashlib.sha256(manifest_raw).hexdigest(), four_qualities=qualities)
        await self.objects.put(descriptor["bundle_key"], bundle)
        await self.objects.put(self._key(request, "descriptor.json"), canonical(descriptor))
        return descriptor

    async def _checked_at(self, request: dict[str, Any]) -> str:
        """首次真实检测时间持久化，崩溃重试复用原检测批次。"""
        key = self._key(request, "check-batch.json")
        try:
            raw = await self.objects.get(key)
        except FinanceError as error:
            if error.code != "OBJECT_NOT_FOUND":
                raise
            raw = canonical({"checked_at": datetime.now(UTC).isoformat()})
            try:
                await self.objects.put(key, raw)
            except FinanceError as conflict:
                if conflict.code != "OBJECT_IMMUTABLE":
                    raise
            raw = await self.objects.get(key)
        batch: dict[str, Any] = json.loads(raw, object_pairs_hook=_unique_object)
        return str(batch["checked_at"])

    async def _descriptor(self, request: dict[str, Any]) -> dict[str, Any]:
        """查单恢复只读取已固定材料，不能重新收集或上传。"""
        raw = await self.objects.get(self._key(request, "descriptor.json"))
        descriptor: dict[str, Any] = json.loads(raw, object_pairs_hook=_unique_object)
        return descriptor

    async def submit(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """发送前已完成preflight；网络未知由持久队列接管查单。"""
        if request["action"] == "audit_archive":
            return await self._audit(request)
        descriptor = await self._descriptor(request)
        bundle = await self.objects.get(descriptor["bundle_key"])
        if hashlib.sha256(bundle).hexdigest() != descriptor["bundle_sha256"]:
            raise FinanceError("ARCHIVE_STAGING_CORRUPT", 409, "固定物化包摘要不符")
        result, raw = await self.http.exchange(request, bundle)
        if result["status"] == "succeeded":
            result = await self.http.verify(request, result, raw, descriptor)
        return result, raw

    async def query(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """成功恢复也必须重新核对全部实际版本与锁。"""
        if request["action"] == "audit_archive":
            return await self._audit(request)
        result, raw = await self.http.exchange(request)
        if result["status"] == "succeeded":
            try:
                descriptor = await self._descriptor(request)
            except FinanceError as error:
                preserved = ProviderResponseError(raw, "ARCHIVE_STAGING_UNAVAILABLE")
                preserved.readback_evidence = [{"code": error.code}]
                raise preserved from error
            result = await self.http.verify(request, result, raw, descriptor)
        return result, raw

    async def _audit(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """审计只读取原包并保留原始渠道回执，新任务不上传。"""
        original = request["payload"]["archive_request"]
        result, raw = await self.query(original)
        from xuanpu_finance.settlement.provider import RESULT_SCOPE

        scoped = {field: request[field] for field in RESULT_SCOPE}
        scoped.update(status=result["status"], evidence=result.get("evidence", []),
            outcome=result.get("outcome"))
        return scoped, raw
