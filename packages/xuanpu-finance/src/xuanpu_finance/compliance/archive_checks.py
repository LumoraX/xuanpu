"""真实材料检测和确定性清册构造，仅支持明确的隔离profile。"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import TYPE_CHECKING, Any

import anyio
from anyio import to_process

from xuanpu_finance.compliance.document_formats import check_document
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.invoices.parser import _tree
from xuanpu_finance.operations import request_digest
from xuanpu_finance.settlement.provider import _unique_object

if TYPE_CHECKING:
    from xuanpu_finance.compliance.archive_models import ArchiveArtifact, ArchiveSnapshot
    from xuanpu_finance.compliance.evidence import EvidenceFetcher
    from xuanpu_finance.objects import OriginalObjects

SIMULATION_PROFILE = "simulation-archive-v1"
PARSE_TIMEOUT_SECONDS = 10


def canonical(value: object) -> bytes:
    """摘要和归档文件采用同一确定性UTF8编码。"""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def archive_profile_eligible(simulation: bool, profile_id: object, profile_version: object,
                             lock_mode: object) -> bool:
    """归档资格只接受经过隔离验收的固定profile、版本及锁模式。"""
    return simulation and (profile_id, profile_version, lock_mode) == (SIMULATION_PROFILE, 1, "SIMULATION")


def validate_profile(snapshot: ArchiveSnapshot, request: dict[str, Any]) -> None:
    """没有真实标准材料和检测器时正式归档保持关闭。"""
    if not archive_profile_eligible(request["simulation"] and snapshot.simulation,
                                    snapshot.profile.id, snapshot.profile.version,
                                    snapshot.retention.required_lock_mode):
        raise FinanceError("ARCHIVE_PROFILE_UNVERIFIED", 409, "当前仅支持明确隔离的模拟归档profile")
    if snapshot.target != request["target"]:
        raise FinanceError("ARCHIVE_SCOPE_MISMATCH", 409, "归档输入目标与固定请求不符")
    unhashed = snapshot.model_dump(exclude={"source_snapshot_hash"}, exclude_unset=True)
    if request_digest(unhashed) != snapshot.source_snapshot_hash:
        raise FinanceError("ARCHIVE_SNAPSHOT_MISMATCH", 409, "归档冻结输入摘要不符")


def check_parse(raw: bytes, media_type: str, kind: str | None = None) -> None:
    """实际解析支持的文件格式，未知二进制不能虚构可用性通过。"""
    if media_type == "application/octet-stream" and kind in {"tax_receipt", "settlement_raw_receipt"}:
        # 空HTTP错误体仍是可精确恢复的原始证据，不推断任何业务成功。
        return
    elif media_type in {"application/pdf", "image/png", "image/jpeg"}:
        check_document(raw,media_type)
    elif media_type in {"application/xml", "text/xml"}:
        _tree(raw)
    elif media_type == "application/json":
        json.loads(raw, object_pairs_hook=_unique_object)
    elif media_type == "text/plain":
        raw.decode("utf-8")
    else:
        raise FinanceError("ARCHIVE_FORMAT_UNVERIFIED", 409, "该格式缺少已验证可用性检测器")


async def materialize(artifact: ArchiveArtifact, objects: OriginalObjects,
                      fetcher: EvidenceFetcher | None) -> bytes:
    """读取实际原件或渠道材料，并校验原始字节。"""
    source = artifact.source
    if "object_key" in source:
        raw = await objects.get(source["object_key"])
    elif "content_base64" in source:
        try:
            raw = base64.b64decode(source["content_base64"], validate=True)
        except (ValueError, TypeError) as error:
            raise FinanceError("ARCHIVE_CONTENT_INVALID", 422, "材料编码无效") from error
    elif fetcher is not None:
        raw = await fetcher.fetch(source)
    else:
        raise FinanceError("ARCHIVE_EVIDENCE_UNAVAILABLE", 409, "实际证据下载器未配置")
    if len(raw) != artifact.size or hashlib.sha256(raw).hexdigest() != artifact.sha256:
        raise FinanceError("ARCHIVE_MATERIAL_MISMATCH", 409, "归档材料大小或摘要不符")
    try:
        with anyio.fail_after(PARSE_TIMEOUT_SECONDS):
            await to_process.run_sync(check_parse,raw,artifact.media_type,artifact.kind,cancellable=True)
    except TimeoutError as error:
        raise FinanceError("ARCHIVE_PARSE_TIMEOUT",409,"归档格式解析超过执行期限，未取得可用性结论") from error
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise FinanceError("ARCHIVE_PARSE_FAILED", 409, "归档材料解析失败") from error
    return raw


def reports(snapshot: ArchiveSnapshot, checked_at: str) -> list[dict[str, Any]]:
    """报告仅覆盖模拟来源、实际摘要/解析及本地加密，不冒称正式认证。"""
    scope = {
        "authenticity": "simulation来源；不证明官方签名或真实税务效力",
        "integrity": "固定清册、必需材料集合和实际全部字节摘要一致",
        "usability": "结构化成员实际解析；渠道原始回执按只读原字节恢复，不推断其中业务内容",
        "security": "独立AES-GCM副本；最终还需逐版本模拟锁读回，不证明正式WORM或灾备",
    }
    return [dict(name=name, status="passed", detector_version="simulation-checks-v1",
        checked_at=checked_at, input_hash=snapshot.source_snapshot_hash,
        evidence=[evidence]) for name, evidence in scope.items()]
