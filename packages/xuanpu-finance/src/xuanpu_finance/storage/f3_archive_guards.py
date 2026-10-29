"""领域独立核对封存清册、检测输入及逐对象保留事实。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.operations import request_digest

if TYPE_CHECKING:
    from xuanpu_finance.compliance.archive_models import ArchiveMember, ArchiveOutcome


def validate_archive_outcome(outcome: ArchiveOutcome, request: dict[str, Any]) -> None:
    """本地读回成功仍须精确属于固定目标、快照摘要和保留策略。"""
    snapshot = request["payload"]["archive_snapshot"]
    expected_hash = request_digest({k: v for k, v in snapshot.items() if k != "source_snapshot_hash"})
    matches = [outcome.readback_verified, outcome.source_snapshot_hash == expected_hash, snapshot["source_snapshot_hash"] == expected_hash, outcome.parent_manifest_hash == snapshot["parent_manifest_hash"], outcome.standard_profile == snapshot["profile"]["id"], outcome.retention_class == snapshot["retention"]["retention_class"], request["target"] == snapshot["target"]]
    if not all(matches):
        raise FinanceError("ARCHIVE_SCOPE_MISMATCH", 409, "档案读回、目标、来源摘要、标准或父清册不符")
    members = validate_archive_members(outcome, snapshot)
    validate_archive_retention(outcome, snapshot["retention"])
    validate_archive_reports(outcome, expected_hash)
    if members[outcome.manifest_member_id].sha256 != outcome.manifest_hash:
        raise FinanceError("ARCHIVE_MANIFEST_HASH_MISMATCH", 409, "实际清册成员摘要与封存清册hash不符")


def validate_archive_members(outcome: ArchiveOutcome, snapshot: dict[str, Any]) -> dict[str, ArchiveMember]:
    """实际成员集合必须精确等于原材料、清册和独立封存证明。"""
    members = {m.id: m for m in outcome.members}
    seal_ids = {outcome.manifest_member_id, outcome.seal_member_id}
    artifact_ids = {a["id"] for a in snapshot["artifacts"]}
    valid = [len(members) == len(outcome.members), len(seal_ids) == 2, not seal_ids & artifact_ids, set(members) == artifact_ids | seal_ids, len({m.path for m in outcome.members}) == len(outcome.members)]
    if not all(valid):
        raise FinanceError("ARCHIVE_SEAL_REQUIRED", 409, "清册与封存证明必须是不同的实际版本成员且无额外文件")
    if (members[outcome.manifest_member_id].path, members[outcome.seal_member_id].path) != ("manifest.json", "seal-attestation.json"):
        raise FinanceError("ARCHIVE_SEAL_REQUIRED", 409, "清册或独立封存证明路径不符")
    for artifact in snapshot["artifacts"]:
        member = members[artifact["id"]]
        if (member.path, member.sha256, member.size, member.media_type) != (artifact["path"], artifact["sha256"], artifact["size"], artifact["media_type"]):
            raise FinanceError("ARCHIVE_MEMBER_MISMATCH", 409, "实际档案清册缺件或与冻结成员不符")
    return members


def validate_archive_retention(outcome: ArchiveOutcome, retention: dict[str, Any]) -> None:
    """聚合声明和每一具体对象版本都须满足同一固定期限和legal hold。"""
    sealed = datetime.fromisoformat(outcome.sealed_at)
    if sealed.tzinfo is None or sealed > datetime.now(UTC) + timedelta(minutes=5):
        raise FinanceError("ARCHIVE_SEAL_TIME_INVALID", 409, "实际封存时间须明确时区且不能来自未来")
    anchor = datetime.fromisoformat(retention["anchor_at"])
    if retention.get("anchor_rule", "archived_at") == "archived_at":
        anchor = max(anchor, sealed)
    minimum = anchor + timedelta(days=retention["days"])
    actual_until = []
    for member in outcome.members:
        retained = datetime.fromisoformat(member.retained_until)
        if retained.tzinfo is None or retained < minimum or member.lock_mode != retention["required_lock_mode"] or member.legal_hold != retention["required_legal_hold"]:
            raise FinanceError("ARCHIVE_RETENTION_MISMATCH", 409, "对象版本锁、期限或legal hold不满足固定策略")
        actual_until.append(retained)
    overall = datetime.fromisoformat(outcome.retained_until)
    if overall.tzinfo is None or overall != min(actual_until) or outcome.legal_hold != retention["required_legal_hold"]:
        raise FinanceError("ARCHIVE_RETENTION_MISMATCH", 409, "聚合期限或legal hold不等于实际成员保留事实")


def validate_archive_reports(outcome: ArchiveOutcome, expected_hash: str) -> None:
    """四性检测不仅称通过，还须记录本快照的实际检测器和带时区时间。"""
    if {q.name for q in outcome.four_qualities} != {"authenticity", "integrity", "usability", "security"}:
        raise FinanceError("ARCHIVE_QUALITY_FAILED", 409, "四性检测项目不完整")
    for report in outcome.four_qualities:
        checked = datetime.fromisoformat(report.checked_at)
        valid = [report.status == "passed", report.input_hash == expected_hash, bool(report.detector_version.strip()), all(value.strip() for value in report.evidence), checked.tzinfo is not None]
        if not all(valid):
            raise FinanceError("ARCHIVE_QUALITY_FAILED", 409, "检测结论、固定输入摘要、检测器或证据无效")
        if checked > datetime.now(UTC) + timedelta(minutes=5):
            raise FinanceError("ARCHIVE_QUALITY_FAILED", 409, "检测时间不能来自未来")
