"""HTTP 接口的请求与响应模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import (
        AuditRecord,
        PackageVersionRecord,
        SessionRecord,
        UsageRecord,
    )
    from xuanpu_server.tenancy import ApiKeyRecord, Tenant


class _Request(BaseModel):
    """请求模型的基类：多余的字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")


class QuotaBody(_Request):
    """租户配额。"""

    max_active_sessions: int | None = Field(default=None, ge=1)
    max_daily_tokens: int | None = Field(default=None, ge=1)
    max_concurrent_turns: int | None = Field(default=None, ge=1)


class CreateTenantBody(_Request):
    """新建租户。"""

    tenant_id: str
    name: str = Field(min_length=1, max_length=200)
    quota: QuotaBody = QuotaBody()


class TenantStatusBody(_Request):
    """启用或停用租户。"""

    status: Literal["active", "suspended"]


class IssueKeyBody(_Request):
    """签发 API key。"""

    name: str = Field(min_length=1, max_length=200)
    role: Literal["tenant_admin", "tenant_member"] = "tenant_member"


class SecretBody(_Request):
    """写入密钥。"""

    value: str = Field(min_length=1)


class ActivateBody(_Request):
    """切换生效版本。"""

    version: str


class EvalBody(_Request):
    """运行评测。"""

    version: str | None = None
    names: list[str] | None = None


class CreateSessionBody(_Request):
    """新建会话。"""

    package: str
    version: str | None = None
    entry: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class MessageBody(_Request):
    """发消息。"""

    text: str


class ResumeBody(_Request):
    """送回审批结果或表单答案。键是挂起事件里给出的 ``request_id``。"""

    resolutions: dict[str, Any]


class CancelBody(_Request):
    """停止生成。``submission_id`` 省略时停掉会话里全部在途的对话。"""

    submission_id: str | None = None


def tenant_view(tenant: Tenant) -> dict[str, Any]:
    """租户的对外表示。"""
    return {
        "tenant_id": tenant.tenant_id,
        "name": tenant.name,
        "status": tenant.status,
        "quota": {
            "max_active_sessions": tenant.quota.max_active_sessions,
            "max_daily_tokens": tenant.quota.max_daily_tokens,
            "max_concurrent_turns": tenant.quota.max_concurrent_turns,
        },
        "created_at": tenant.created_at,
    }


def key_view(record: ApiKeyRecord) -> dict[str, Any]:
    """API key 的对外表示。不含密钥摘要。"""
    return {
        "key_id": record.key_id,
        "tenant_id": record.tenant_id,
        "name": record.name,
        "role": record.role,
        "created_at": record.created_at,
        "revoked_at": record.revoked_at,
    }


def package_view(record: PackageVersionRecord, *, active: str | None) -> dict[str, Any]:
    """业务包版本的对外表示。不含服务器上的存放位置。"""
    return {
        "name": record.name,
        "version": record.version,
        "digest": record.digest,
        "active": record.version == active,
        "published_at": record.published_at,
        "published_by": record.published_by,
    }


def session_view(record: SessionRecord) -> dict[str, Any]:
    """会话的对外表示。"""
    return {
        "session_id": record.session_id,
        "package": record.package,
        "version": record.version,
        "entry": record.entry,
        "status": record.status,
        "owner": record.owner,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "created_by": record.created_by,
        "metadata": dict(record.metadata),
    }


def usage_view(record: UsageRecord) -> dict[str, Any]:
    """用量的对外表示。"""
    return {
        "day": record.day,
        "input_tokens": record.input_tokens,
        "output_tokens": record.output_tokens,
        "total_tokens": record.total_tokens,
        "turns": record.turns,
    }


def audit_view(record: AuditRecord) -> dict[str, Any]:
    """审计记录的对外表示。"""
    return {
        "audit_id": record.audit_id,
        "source": record.source,
        "kind": record.kind,
        "at": record.at,
        "actor": record.actor,
        "session_id": record.session_id,
        "stream": record.stream,
        "seq": record.seq,
        "data": record.data,
    }


__all__ = [
    "ActivateBody",
    "CancelBody",
    "CreateSessionBody",
    "CreateTenantBody",
    "EvalBody",
    "IssueKeyBody",
    "MessageBody",
    "QuotaBody",
    "ResumeBody",
    "SecretBody",
    "TenantStatusBody",
    "audit_view",
    "key_view",
    "package_view",
    "session_view",
    "tenant_view",
    "usage_view",
]
