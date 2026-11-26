"""管家造匠（Artisan Forge）与三层不可变发布节点的数据模型（ADR 0025）。"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 —— pydantic 运行时解析
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from spec import API_VERSION

# L2 构造工具层独立版本号：修改 xuanpu.yaml 构造代码时同步递增，用于血缘追溯与双向兼容回归
FORGE_VERSION = "2026.11.27.1"
SPEC_API_VERSION = API_VERSION

ForgeDraftStatus = Literal["editing", "validated", "tested", "reviewed", "published", "discarded"]
ReleaseLayer = Literal["artisan", "forge", "kernel"]
ReleaseState = Literal["active", "retired", "revoked"]


class BuiltByProvenance(BaseModel):
    """制品构造血缘：记录由哪个版本的 L2 构造器与规范基于哪份基线构建。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    forge_version: str = Field(min_length=1, max_length=64)
    spec_version: str = Field(min_length=1, max_length=64)
    base_package_digest: str | None = Field(default=None, max_length=128)


class GateCheckReport(BaseModel):
    """单级门禁验证结果（L1 静态校验 / L2 沙盒单测 / L3 行为评测）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    level: Literal["L1", "L2", "L3"]
    passed: bool
    summary: str = Field(max_length=1000)
    errors: tuple[str, ...] = ()
    checked_at: datetime


class ForgeReviewReport(BaseModel):
    """管家审核结论：绑定 Diff 文件清单、权限变化与 L1/L2/L3 门禁证据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    approved: bool
    summary: str = Field(min_length=1, max_length=2000)
    diff_files: tuple[str, ...] = ()
    permission_changes: tuple[str, ...] = ()
    l1_passed: bool = False
    l2_passed: bool = False
    l3_passed: bool = False
    reviewer: str = Field(min_length=1, max_length=128)
    reviewed_at: datetime


class ForgeDraftRecord(BaseModel):
    """隔离沙盒内的一份造匠草稿。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    draft_id: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    package_name: str = Field(min_length=1, max_length=64)
    base_version: str | None = Field(default=None, max_length=64)
    status: ForgeDraftStatus = "editing"
    built_by: BuiltByProvenance
    l1_report: GateCheckReport | None = None
    l2_report: GateCheckReport | None = None
    l3_report: GateCheckReport | None = None
    review: ForgeReviewReport | None = None
    published_version: str | None = None
    published_digest: str | None = None
    created_at: datetime
    updated_at: datetime


class ReleaseNode(BaseModel):
    """不可变发布节点（L1 匠人 / L2 构造器 / L3 内核通用）：只追加不删除，回退即追加新代次指向旧制品。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    release_id: str = Field(min_length=1, max_length=80)
    tenant_id: str = Field(min_length=1, max_length=128)
    layer: ReleaseLayer
    target_name: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    version: str = Field(min_length=1, max_length=64)
    artifact_digest: str = Field(min_length=1, max_length=128)
    state: ReleaseState = "active"
    supersedes: str | None = Field(default=None, max_length=80)
    rollback_of: str | None = Field(default=None, max_length=80)
    built_by: BuiltByProvenance | None = None
    evidence_ref: str | None = Field(default=None, max_length=160)
    actor: str = Field(min_length=1, max_length=128)
    created_at: datetime
