"""独立档案测试服务，真实解析multipart和ZIP并提供固定版本读回。"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from email.parser import BytesParser
from email.policy import default
from typing import Any

import httpx
from xuanpu_finance.compliance.archive_checks import canonical
from xuanpu_finance.compliance.archive_models import timestamp
from xuanpu_finance.settlement.provider import RESULT_SCOPE


class ArchiveService:
    """隔离服务只模拟版本协议，不能证明真实WORM能力。"""

    def __init__(self, fault: str | None = None) -> None:
        """故障注入在实际对象或锁的读回端生效。"""
        self.fault, self.seen = fault, []
        self.contents: dict[str, bytes] = {}
        self.result: dict[str, Any] = {}
        self.members: list[dict[str, Any]] = []
        self.sealed_at = datetime.now(UTC).isoformat()

    def _member(self, id: str, path: str, raw: bytes, media: str, retention: Any) -> dict[str, Any]:
        """每个对象拥有独立固定版本和模拟保留期限。"""
        self.contents[id] = raw
        return dict(id=id, path=path, version_id="version-"+id,
            sha256=hashlib.sha256(raw).hexdigest(), size=len(raw), media_type=media,
            lock_mode="SIMULATION", retained_until=(max(timestamp(retention["anchor_at"]),timestamp(self.sealed_at))+
                timedelta(days=retention["days"], hours=1)).isoformat(),
            legal_hold=retention["required_legal_hold"])

    def accept(self, req: httpx.Request) -> None:
        """解析真实multipart字节；材料必须已经在ZIP中传到服务。"""
        message = BytesParser(policy=default).parsebytes(
            b"Content-Type: " + req.headers["content-type"].encode()+b"\r\n\r\n"+req.content)
        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                  for part in message.iter_parts()}
        request = json.loads(fields["request"])
        with zipfile.ZipFile(io.BytesIO(fields["bundle"])) as archive:
            manifest_raw = archive.read("manifest.json")
            manifest = json.loads(manifest_raw)
            retention = manifest["retention"]
            self.members = [self._member(m["id"], m["path"], archive.read(m["path"]),
                m["media_type"], retention) for m in manifest["members"]]
        self.members.append(self._member("manifest", "manifest.json", manifest_raw,
                                       "application/json", retention))
        manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
        seal = dict(manifest_hash=manifest_hash, sealed_at=self.sealed_at, source_snapshot_hash=manifest["source_snapshot_hash"],
                    members=list(self.members), four_qualities=manifest["four_qualities"])
        if self.fault == "seal":
            seal["manifest_hash"] = "b"*64
        self.members.append(self._member("seal", "seal-attestation.json", canonical(seal),
                                       "application/json", retention))
        outcome = dict(kind="archive", package_id="package1", sealed_at=self.sealed_at, manifest_hash=manifest_hash,
            manifest_member_id="manifest", seal_member_id="seal", standard_profile=manifest["profile"]["id"],
            retention_class=retention["retention_class"], retained_until=self.members[0]["retained_until"],
            legal_hold=retention["required_legal_hold"], members=self.members,
            four_qualities=manifest["four_qualities"], parent_manifest_hash=manifest["parent_manifest_hash"],
            source_snapshot_hash=manifest["source_snapshot_hash"], readback_verified=True)
        if self.fault == "aggregate_retention":
            outcome["retained_until"] = "2099-01-01T00:00:00+00:00"
        self.result = {**{k: request[k] for k in RESULT_SCOPE}, "status": "succeeded", "outcome": outcome,
            "evidence": [dict(id="archive-receipt",sha256=manifest_hash,media_type="application/json",object_ref="test-only")]}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        """所有后续请求均走固定GET，错版本不会返回当前版本。"""
        self.seen.append((req.method, req.url.path))
        if req.method == "POST":
            self.accept(req)
            return httpx.Response(200, json=self.result)
        if "/objects/" not in req.url.path:
            return httpx.Response(200, json=self.result)
        member_id = req.url.path.split("/objects/",1)[1].split("/")[0]
        member = next(m for m in self.members if m["id"] == member_id)
        if req.url.params.get("version_id") != member["version_id"]:
            return httpx.Response(404)
        if req.url.path.endswith("/retention"):
            lock = {k:member[k] for k in ("version_id","sha256","size","lock_mode","retained_until","legal_hold")}
            if self.fault == "lock":
                lock["lock_mode"] = "GOVERNANCE"
            if self.fault == "hold":
                lock["legal_hold"] = not lock["legal_hold"]
            return httpx.Response(200,json=lock)
        if self.fault == "missing":
            return httpx.Response(404)
        raw = self.contents[member_id] + (b" " if self.fault == "hash" else b"")
        return httpx.Response(200,content=raw,headers={"content-type":member["media_type"]})
