"""通用档案HTTP契约：实际ZIP上传、固定版本读取及锁核对。"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from xuanpu_finance.compliance.archive_checks import canonical
from xuanpu_finance.compliance.archive_models import ArchiveOutcome, ArchiveSnapshot, timestamp
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.settlement.provider import (
    MAX_RECEIPT_BYTES,
    ProviderResponseError,
    _unique_object,
)

if TYPE_CHECKING:
    from xuanpu_finance.compliance.archive_models import ArchiveMember
    from xuanpu_finance.compliance.provider import ComplianceRegistry


class ArchiveHTTP:
    """仅从受控origin访问固定档案路径，不接受返回URL。"""

    def __init__(self, registry: ComplianceRegistry) -> None:
        """复用显式渠道版本、凭据和异步客户端。"""
        self.registry = registry

    async def exchange(self, request: dict[str, Any], bundle: bytes | None = None
                       ) -> tuple[dict[str, Any], bytes]:
        """有bundle时首次提交，查单不包含上传内容。"""
        config = self.registry._request_config(request, submit=bundle is not None)
        headers = {"Authorization": "Bearer " + self.registry.tokens[(config.provider,
            config.configuration_version)], "Idempotency-Key": request["external_request_id"]}
        path = "/v1/archives"
        kwargs: dict[str, Any] = {}
        if bundle is not None:
            kwargs["files"] = {"request": (None, canonical(request), "application/json"),
                               "bundle": ("bundle.zip", bundle, "application/zip")}
        else:
            path += "/" + quote(request["external_request_id"], safe="")
        async with self.registry.client.stream("POST" if bundle is not None else "GET",
            config.base_url + path, headers=headers, follow_redirects=False, **kwargs) as response:
            raw = await self._read(response, MAX_RECEIPT_BYTES)
            if response.status_code not in {200, 201, 202} and not (
                bundle is None and response.status_code == 404):
                raise ProviderResponseError(raw, "ARCHIVE_HTTP_ERROR")
        return self.registry._validated_result(request, raw), raw

    async def _read(self, response: Any, maximum: int) -> bytes:
        """流式有界读取，拒绝大文件而不隐式截断。"""
        raw = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > maximum:
                    raise ProviderResponseError(bytes(raw[:maximum]), "ARCHIVE_RESPONSE_TOO_LARGE")
        except httpx.HTTPError as error:
            if raw:
                raise ProviderResponseError(bytes(raw), "ARCHIVE_NETWORK_UNKNOWN") from error
            raise
        return bytes(raw)

    async def object(self, request: dict[str, Any], member: ArchiveMember, *, retention: bool
                     ) -> bytes:
        """明确version_id取回对象或其锁，禁止当前最新版替代旧版。"""
        config = self.registry._request_config(request, submit=False)
        path = "/v1/archives/" + quote(request["external_request_id"], safe="")
        path += "/objects/" + quote(member.id, safe="") + ("/retention" if retention else "")
        headers = {"Authorization": "Bearer " + self.registry.tokens[(config.provider,
            config.configuration_version)]}
        async with self.registry.client.stream("GET", config.base_url + path, headers=headers,
            params={"version_id": member.version_id}, follow_redirects=False) as response:
            raw = await self._read(response, MAX_RECEIPT_BYTES if retention else 20*1024*1024)
            if response.status_code != 200:
                raise ProviderResponseError(raw, "ARCHIVE_OBJECT_UNAVAILABLE")
            media = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if not retention and media != member.media_type:
                raise ProviderResponseError(raw, "ARCHIVE_MEDIA_MISMATCH")
        return raw

    async def verify(self, request: dict[str, Any], result: dict[str, Any], raw: bytes,
                     expected: dict[str, Any]) -> dict[str, Any]:
        """每条成功路径都全量核对内容、清册、证明和各对象保留锁。"""
        content: dict[str, bytes] = {}
        try:
            outcome = ArchiveOutcome.model_validate(result["outcome"])
            snapshot = ArchiveSnapshot.model_validate(request["payload"]["archive_snapshot"])
            self._scope(outcome, snapshot, expected)
            for member in outcome.members:
                content[member.id] = await self._verify_member(request, member, snapshot, outcome.sealed_at)
            self._manifest_and_seal(outcome, content, expected)
            return {**result, "outcome": outcome.model_copy(update={"readback_verified": True}).model_dump()}
        except ProviderResponseError as error:
            main = ProviderResponseError(raw, "ARCHIVE_READBACK_INVALID")
            main.readback_evidence = [{"code": error.code,
                "sha256": hashlib.sha256(error.raw).hexdigest(),
                "content_base64": base64.b64encode(error.raw).decode("ascii")}]
            raise main from error
        except (ValidationError, ValueError, KeyError, RecursionError) as error:
            main = ProviderResponseError(raw, "ARCHIVE_READBACK_INVALID")
            for member_id in (result.get("outcome", {}).get("manifest_member_id"),
                              result.get("outcome", {}).get("seal_member_id")):
                if member_id in content:
                    evidence_raw = content[member_id]
                    main.readback_evidence.append({"member_id": member_id,
                        "sha256": hashlib.sha256(evidence_raw).hexdigest(),
                        "content_base64": base64.b64encode(evidence_raw).decode("ascii")})
            raise main from error

    def _scope(self, outcome: ArchiveOutcome, snapshot: ArchiveSnapshot,
               expected: dict[str, Any]) -> None:
        """冻结策略独立于供应商声明，所有成员集合必须完全闭合。"""
        if (outcome.manifest_hash != expected["manifest_hash"] or
            outcome.source_snapshot_hash != snapshot.source_snapshot_hash or
            outcome.standard_profile != snapshot.profile.id or
            outcome.retention_class != snapshot.retention.retention_class or
            outcome.parent_manifest_hash != snapshot.parent_manifest_hash or
            outcome.legal_hold != snapshot.retention.required_legal_hold or
            outcome.manifest_member_id == outcome.seal_member_id):
            raise ValueError("档案scope不符")
        sealed = timestamp(outcome.sealed_at)
        if sealed > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("封存时刻不能来自未来")
        minimum_retention = min(timestamp(member.retained_until) for member in outcome.members)
        if timestamp(outcome.retained_until) != minimum_retention:
            raise ValueError("归档聚合保留期与成员实际锁不符")
        names = [member.id for member in outcome.members]
        paths = [member.path for member in outcome.members]
        if len(set(names)) != len(names) or len(set(paths)) != len(paths):
            raise ValueError("档案版本清册重复")
        expected_members = {a.id: (a.path, a.sha256, a.size, a.media_type) for a in snapshot.artifacts}
        actual = {m.id: (m.path, m.sha256, m.size, m.media_type) for m in outcome.members
                  if m.id not in {outcome.manifest_member_id, outcome.seal_member_id}}
        if actual != expected_members:
            raise ValueError("档案成员集合不符")
        if [q.model_dump() for q in outcome.four_qualities] != expected["four_qualities"]:
            raise ValueError("档案检测不符")
        if {q.name for q in outcome.four_qualities} != {
            "authenticity", "integrity", "usability", "security"} or any(
            q.status != "passed" for q in outcome.four_qualities):
            raise ValueError("档案检测未通过")

    async def _verify_member(self, request: dict[str, Any], member: ArchiveMember,
                             snapshot: ArchiveSnapshot, sealed_at: str) -> bytes:
        """逐字节读取且逐版本取锁，与受控期限和模式独立比较。"""
        raw, lock_raw = b"", b""
        try:
            raw = await self.object(request, member, retention=False)
            lock_raw = await self.object(request, member, retention=True)
            lock = json.loads(lock_raw, object_pairs_hook=_unique_object)
            expected_lock = {key: getattr(member, key) for key in (
                "version_id", "sha256", "size", "lock_mode", "retained_until", "legal_hold")}
            if lock != expected_lock or len(raw) != member.size or (
                hashlib.sha256(raw).hexdigest() != member.sha256):
                raise ValueError("版本内容或锁不符")
            anchor = timestamp(snapshot.retention.anchor_at)
            if snapshot.retention.anchor_rule == "archived_at":
                anchor = max(anchor, timestamp(sealed_at))
            minimum = anchor + timedelta(days=snapshot.retention.days)
            if timestamp(member.retained_until) < minimum or (
                member.lock_mode != snapshot.retention.required_lock_mode or
                member.legal_hold != snapshot.retention.required_legal_hold):
                raise ValueError("保留期、锁模式或法律保留不符")
            return raw
        except (httpx.HTTPError, FinanceError, ValueError, KeyError, RecursionError) as error:
            code = error.code if isinstance(error, FinanceError) else type(error).__name__
            evidence = {"member_id": member.id, "version_id": member.version_id, "code": code,
                "object_base64": base64.b64encode(raw).decode("ascii"),
                "retention_base64": base64.b64encode(lock_raw).decode("ascii")}
            if isinstance(error, ProviderResponseError):
                evidence["partial_base64"] = base64.b64encode(error.raw).decode("ascii")
            raise ProviderResponseError(canonical(evidence), "ARCHIVE_MEMBER_INVALID") from error

    def _manifest_and_seal(self, outcome: ArchiveOutcome, content: dict[str, bytes],
                           expected: dict[str, Any]) -> None:
        """清册不自含摘要，独立封存证明核对内容版本及其锁。"""
        manifest = content[outcome.manifest_member_id]
        if hashlib.sha256(manifest).hexdigest() != outcome.manifest_hash or (
            manifest != canonical(expected["manifest"])):
            raise ValueError("清册实际内容不符")
        seal = json.loads(content[outcome.seal_member_id], object_pairs_hook=_unique_object)
        versions = [m.model_dump() for m in outcome.members if m.id != outcome.seal_member_id]
        expected_seal = dict(manifest_hash=outcome.manifest_hash, sealed_at=outcome.sealed_at,
            source_snapshot_hash=outcome.source_snapshot_hash, members=versions,
            four_qualities=[q.model_dump() for q in outcome.four_qualities])
        if seal != expected_seal:
            raise ValueError("独立封存证明不符")
