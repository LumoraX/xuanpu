"""档案清册、保留策略和逐版本封存证据的严格契约。"""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArchiveModel(BaseModel):
    """档案数据拒绝隐式类型转换和未知字段。"""

    model_config = ConfigDict(extra="forbid", strict=True)


def timestamp(value: str) -> datetime:
    """所有期限须明确时区，避免本地时间误解释。"""
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("时间缺少时区")
    return result


class ArchiveArtifact(ArchiveModel):
    """清册成员引用实际文件来源，不以外部URL代替文件。"""

    id: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=512)
    kind: str = Field(min_length=1, max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=128)
    size: int = Field(ge=0, le=20*1024*1024)
    source: dict[str, Any]

    @model_validator(mode="after")
    def safe_source(self) -> Self:
        """限制安全相对路径和明确互斥的来源类型。"""
        if self.size == 0 and self.kind not in {"tax_receipt", "settlement_raw_receipt"}:
            raise ValueError("仅受控渠道原始空回执允许零字节")
        path = PurePosixPath(self.path)
        if path.is_absolute() or "\\" in self.path or any(
            part in {"", ".", ".."} for part in self.path.split("/")
        ) or self.path in {"manifest.json", "seal-attestation.json"}:
            raise ValueError("档案成员路径无效")
        keys = set(self.source)
        allowed = ({"object_key"}, {"content_base64"},
                   {"provider_request", "evidence", "provider_kind"})
        if keys not in allowed:
            raise ValueError("档案成员来源无效")
        return self


class ArchiveProfile(ArchiveModel):
    """冻结标准profile和版本，不能把派生JSON称作官方标准。"""

    id: str = Field(min_length=1)
    version: int = Field(ge=1)


class ArchiveRetention(ArchiveModel):
    """受控策略给出期望锁，不能以渠道回显作为期望值。"""

    retention_class: str = Field(min_length=1)
    days: int = Field(ge=1, le=36500)
    anchor_at: str
    anchor_rule: Literal["archived_at", "invoice_issued_at", "period_end"] = "archived_at"
    required_lock_mode: Literal["SIMULATION", "COMPLIANCE"]
    required_legal_hold: bool


class ArchiveSnapshot(ArchiveModel):
    """完整冻结输入供物化与重启恢复使用，不在发送后读取新事实。"""

    target: dict[str, Any]
    content_version: int = Field(ge=1)
    compliance_version: int = Field(ge=1)
    event_cutoff: str
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    simulation: bool
    profile: ArchiveProfile
    retention: ArchiveRetention
    parent_manifest_hash: str | None = None
    snapshots: dict[str, Any]
    required_roles: list[str] = Field(min_length=1, max_length=100)
    artifacts: list[ArchiveArtifact] = Field(min_length=1, max_length=998)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        """限制总量和重复路径，禁止清册覆盖。"""
        timestamp(self.event_cutoff)
        timestamp(self.retention.anchor_at)
        if len({a.id for a in self.artifacts}) != len(self.artifacts) or len(
            {a.path for a in self.artifacts}) != len(self.artifacts):
            raise ValueError("档案成员标识或路径重复")
        if sum(a.size for a in self.artifacts) > 100*1024*1024:
            raise ValueError("档案材料超过100MiB")
        if not set(self.required_roles) <= {a.kind for a in self.artifacts}:
            raise ValueError("档案缺少该阶段必需材料")
        return self


class QualityReport(ArchiveModel):
    """检测结论精确记录范围，不把未验证项写成通过。"""

    name: Literal["authenticity", "integrity", "usability", "security"]
    status: Literal["passed", "failed", "unavailable", "not_checked", "not_applicable"]
    detector_version: str
    checked_at: str
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence: list[str] = Field(min_length=1)


class ArchiveMember(ArchiveModel):
    """锁和摘要绑定具体对象版本，禁止以可变名称冒充封存。"""

    id: str = Field(min_length=1, max_length=128)
    path: str
    version_id: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=20*1024*1024)
    media_type: str
    lock_mode: Literal["SIMULATION", "COMPLIANCE"]
    retained_until: str
    legal_hold: bool


class ArchiveOutcome(ArchiveModel):
    """本地完成全部读回后才设置readback_verified。"""

    kind: Literal["archive"]
    package_id: str = Field(min_length=1)
    sealed_at: str
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_member_id: str
    seal_member_id: str
    standard_profile: str
    retention_class: str
    retained_until: str
    legal_hold: bool
    members: list[ArchiveMember] = Field(min_length=3, max_length=1000)
    four_qualities: list[QualityReport] = Field(min_length=4, max_length=4)
    parent_manifest_hash: str | None = None
    source_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    readback_verified: bool = False
