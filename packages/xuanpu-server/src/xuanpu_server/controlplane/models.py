"""控制面的记录类型。全部不可变；改动状态时生成新记录写回存储。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SessionStatus = Literal["active", "closed"]
AuditSource = Literal["engine", "gate", "control"]


@dataclass(frozen=True)
class PackageVersionRecord:
    """业务包的一个已发布版本。发布后不可修改。

    Attributes:
        tenant_id: 所属租户。
        name: 包名。
        version: 版本号。
        digest: 归档的指纹。
        manifest_yaml: 清单原文。
        location: 归档在对象存储里的键。
        published_at: 发布时间。
        published_by: 发布者。
    """

    tenant_id: str
    name: str
    version: str
    digest: str
    manifest_yaml: str
    location: str
    published_at: float
    published_by: str


@dataclass(frozen=True)
class SecretRecord:
    """一条密钥。存的是密文。"""

    tenant_id: str
    name: str
    ciphertext: bytes
    updated_at: float
    updated_by: str


@dataclass(frozen=True)
class SessionRecord:
    """一个会话。

    Attributes:
        session_id: 会话标识。
        tenant_id: 所属租户。
        package: 业务包名。
        version: 业务包版本。会话创建时钉死，之后包切换版本不影响已有会话。
        entry: 入口 skill。
        thread_id: 内核里的线程标识；引擎首次创建后回填。
        status: 状态。
        owner: 当前持有会话的实例；无人持有为 None。
        lease_expires_at: 持有的到期时刻。
        created_at: 创建时间。
        updated_at: 最近活动时间。
        created_by: 创建者。
        metadata: 业务方附加的关联标识（客户号、工单号等），原样返回并写进审计；
            平台不解释它的含义。
    """

    session_id: str
    tenant_id: str
    package: str
    version: str
    entry: str
    created_at: float
    updated_at: float
    created_by: str
    thread_id: str | None = None
    status: SessionStatus = "active"
    owner: str | None = None
    lease_expires_at: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    def held_elsewhere(self, instance_id: str, now: float) -> bool:
        """在 ``now`` 这一刻是否由 ``instance_id`` 之外的实例持有（租约未到期）。

        与 Postgres 实现里取得租约的条件互为否定：无人持有、本实例持有或租约已到期，
        都可以取得。
        """
        return (
            self.owner is not None and self.owner != instance_id and self.lease_expires_at > now
        )


@dataclass(frozen=True)
class UsageRecord:
    """某租户某一天的用量。"""

    tenant_id: str
    day: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    turns: int = 0


@dataclass(frozen=True)
class AuditRecord:
    """一条审计记录。只追加，不改不删（ADR 0004 决策 4）。

    Attributes:
        tenant_id: 所属租户。
        source: 来源。``engine`` 是内核事件总线，``gate`` 是效应面闸门，
            ``control`` 是控制面操作。
        kind: 事件类型。
        at: 发生时间。
        actor: 触发者。引擎事件是 ``engine``，其余是调用者标识。
        session_id: 关联的会话。
        stream: 引擎事件所属的事件流。引擎每次被创建是一条新的事件流，序号从头计。
        seq: 引擎事件在事件流里的序号。``(stream, seq)`` 唯一，重复写入去重。
        data: 事件内容。不含密钥明文。
        audit_id: 存储分配的标识，同时是查询的翻页位置。
    """

    tenant_id: str
    source: AuditSource
    kind: str
    at: float
    actor: str
    session_id: str | None = None
    stream: str | None = None
    seq: int | None = None
    data: dict[str, Any] = field(default_factory=dict)
    audit_id: int = 0


@dataclass(frozen=True)
class AuditQuery:
    """审计查询条件。各条件是 AND 关系。

    Attributes:
        session_id: 只看某个会话。
        submission_id: 只看某一轮对话（记录 ``data.submission_id`` 等于它的）。
        kind: 只看某种事件。
        source: 只看某个来源。
        since: 只看这个时刻（含）之后的。
        until: 只看这个时刻（不含）之前的。
        after_id: 翻页位置：只看标识大于它的记录。
        limit: 最多返回多少条。
    """

    session_id: str | None = None
    submission_id: str | None = None
    kind: str | None = None
    source: AuditSource | None = None
    since: float | None = None
    until: float | None = None
    after_id: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        """限制单页大小。"""
        if not 1 <= self.limit <= 1000:
            raise ValueError(f"limit 必须在 1 到 1000 之间，得到 {self.limit}")

    def matches(self, record: AuditRecord) -> bool:
        """某条记录是否命中（不含翻页位置与条数）。"""
        checks = (
            self.session_id is None or record.session_id == self.session_id,
            self.submission_id is None or record.data.get("submission_id") == self.submission_id,
            self.kind is None or record.kind == self.kind,
            self.source is None or record.source == self.source,
            self.since is None or record.at >= self.since,
            self.until is None or record.at < self.until,
        )
        return all(checks)


__all__ = [
    "AuditQuery",
    "AuditRecord",
    "AuditSource",
    "PackageVersionRecord",
    "SecretRecord",
    "SessionRecord",
    "SessionStatus",
    "UsageRecord",
]
