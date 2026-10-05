"""HTTP 接口的请求与响应模型。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.identity.config import MAX_SESSION_TTL_SECONDS, MIN_SESSION_TTL_SECONDS
from xuanpu_server.tenancy import QuotaLimits

if TYPE_CHECKING:
    from xuanpu_server.controlplane.models import (
        AuditRecord,
        PackageVersionRecord,
        SessionRecord,
        UsageRecord,
    )
    from xuanpu_server.identity.models import UserRecord, UserUsageRecord
    from xuanpu_server.tenancy import ApiKeyRecord, Tenant


class _Request(BaseModel):
    """请求模型的基类：多余的字段一律拒绝。"""

    model_config = ConfigDict(extra="forbid")


class QuotaBody(_Request):
    """租户配额：新建租户时的 ``quota``，也是修改配额的请求体。省略或为 null 的项表示不限。"""

    max_active_sessions: int | None = Field(default=None, ge=1)
    max_daily_tokens: int | None = Field(default=None, ge=1)
    max_concurrent_turns: int | None = Field(default=None, ge=1)

    def limits(self) -> QuotaLimits:
        """转成平台的配额对象。"""
        return QuotaLimits(
            max_active_sessions=self.max_active_sessions,
            max_daily_tokens=self.max_daily_tokens,
            max_concurrent_turns=self.max_concurrent_turns,
        )


class CreateTenantBody(_Request):
    """新建租户。"""

    tenant_id: str
    name: str = Field(min_length=1, max_length=200)
    quota: QuotaBody = QuotaBody()


class TenantStatusBody(_Request):
    """启用或停用租户。"""

    status: Literal["active", "suspended"]


class IssueKeyBody(_Request):
    """签发 API key。``act_as_users`` 为真时授予代员工调用的能力（只有管理员能授予）。"""

    name: str = Field(min_length=1, max_length=200)
    role: Literal["tenant_admin", "tenant_member"] = "tenant_member"
    act_as_users: bool = False


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
    """发消息；附件只接受个人空间标识，不接受任意路径或正文。"""

    text: str
    attachments: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list, max_length=8)


class ResumeBody(_Request):
    """送回审批结果或表单答案。键是挂起事件里给出的 ``request_id``。"""

    resolutions: dict[str, Any]


class CancelBody(_Request):
    """停止生成。``submission_id`` 省略时停掉会话里全部在途的对话。

    长度设上限（内核的 submission 标识是 ``sub_`` 加 12 位十六进制），超长输入在校验处
    就拒绝，错误信息里不回显它。
    """

    submission_id: str | None = Field(default=None, min_length=1, max_length=64)


class FeishuConfigBody(_Request):
    """租户的飞书配置（整体替换）。``*_ref`` 是保管库里密钥的名字，不是明文。

    App Secret 必填；两个事件引用可省略或为空，只有两者齐备才启用加密事件。

    ``session_ttl_seconds`` 省略或为 null 时用平台默认（``identity.session_ttl_seconds``）；取值
    范围与平台配置相同：60 到 2592000（30 天）。
    """

    app_id: str = Field(min_length=1, max_length=128)
    app_secret_ref: str
    encrypt_key_ref: str | None = None
    verification_token_ref: str | None = None
    session_ttl_seconds: int | None = Field(
        default=None, ge=MIN_SESSION_TTL_SECONDS, le=MAX_SESSION_TTL_SECONDS
    )

    @field_validator("encrypt_key_ref", "verification_token_ref")
    @classmethod
    def normalize_event_ref(cls, value: str | None) -> str | None:
        """可选事件引用的空字符串与未配置统一存为 None。"""
        return None if value == "" else value


class UserRoleBody(_Request):
    """改员工的角色。"""

    role: Literal["tenant_admin", "tenant_member"]


class UserStatusBody(_Request):
    """停用或启用员工。"""

    status: Literal["active", "disabled"]


def user_view(user: UserRecord) -> dict[str, Any]:
    """员工的对外表示：只有平台里的资料与状态，没有外部身份、登录会话之类的东西。"""
    return {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "role": user.role,
        "status": user.status,
        "disabled_reason": user.disabled_reason,
        "created_at": user.created_at,
        "last_login_at": user.last_login_at,
        "disabled_at": user.disabled_at,
        "unverified_since": user.unverified_since,
    }


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
        "act_as_users": record.can_act_as_user,
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
        "user_id": record.user_id,
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


def user_usage_view(record: UserUsageRecord) -> dict[str, Any]:
    """按员工的用量的对外表示：比租户用量多一个 ``user_id``。"""
    return {
        "day": record.day,
        "user_id": record.user_id,
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
        "user_id": record.user_id,
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
    "UserRoleBody",
    "UserStatusBody",
    "audit_view",
    "key_view",
    "package_view",
    "session_view",
    "tenant_view",
    "usage_view",
    "user_usage_view",
    "user_view",
]
