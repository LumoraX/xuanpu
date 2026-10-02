"""控制面存储的契约测试：内存实现与 Postgres 实现各跑一遍。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from xuanpu_server.controlplane import (
    AuditQuery,
    AuditRecord,
    ControlPlaneStore,
    PackageVersionRecord,
    SecretRecord,
    SessionRecord,
)
from xuanpu_server.errors import ConflictError, NotFoundError, SessionBusyError
from xuanpu_server.tenancy import ApiKeyRecord, QuotaLimits, Tenant

if TYPE_CHECKING:
    from dataclasses import dataclass  # noqa: F401


async def _tenants(store: ControlPlaneStore, *ids: str) -> None:
    """建几个租户。"""
    for tenant_id in ids:
        await store.create_tenant(
            Tenant(tenant_id=tenant_id, name=tenant_id.upper(), created_at=1.0)
        )


def _package(tenant: str, name: str, version: str, at: float = 1.0) -> PackageVersionRecord:
    """构造业务包版本记录。"""
    return PackageVersionRecord(
        tenant_id=tenant,
        name=name,
        version=version,
        digest="sha256:" + "0" * 64,
        manifest_yaml="apiVersion: xuanpu/v1\n",
        location=f"tenants/{tenant}/packages/{name}/{version}/{'0' * 64}.zip",
        published_at=at,
        published_by="key:abc",
    )


def _session(tenant: str, session_id: str, at: float = 1.0, **kwargs: object) -> SessionRecord:
    """构造会话记录。"""
    return SessionRecord(
        session_id=session_id,
        tenant_id=tenant,
        package="support",
        version="1.0.0",
        entry="triage",
        created_at=at,
        updated_at=at,
        created_by="key:abc",
        **kwargs,  # type: ignore[arg-type]
    )


def _engine_event(
    tenant: str, seq: int, *, stream: str = "st-1", session: str = "s1"
) -> AuditRecord:
    """构造一条引擎事件的审计记录。"""
    return AuditRecord(
        tenant_id=tenant,
        source="engine",
        kind="tool_call_completed",
        at=100.0 + seq,
        actor="engine",
        session_id=session,
        stream=stream,
        seq=seq,
        data={"n": seq, "文本": "引号 ' \" 都要原样保存"},
    )


def test_implements_protocol(store: ControlPlaneStore) -> None:
    """实现满足协议。"""
    assert isinstance(store, ControlPlaneStore)


# --- 租户与凭据 ---


async def test_tenant_lifecycle(store: ControlPlaneStore) -> None:
    """建、取、列、改；重复创建与改不存在的租户都报错。"""
    tenant = Tenant(
        tenant_id="acme",
        name="Acme 公司",
        quota=QuotaLimits(max_active_sessions=5, max_daily_tokens=10**12, max_concurrent_turns=8),
        created_at=123.5,
    )
    await store.create_tenant(tenant)
    assert await store.get_tenant("acme") == tenant
    assert await store.get_tenant("nope") is None
    with pytest.raises(ConflictError):
        await store.create_tenant(tenant)

    await _tenants(store, "zeta", "beta")
    assert [item.tenant_id for item in await store.list_tenants()] == ["acme", "beta", "zeta"]

    suspended = Tenant(tenant_id="acme", name="Acme", status="suspended", created_at=123.5)
    await store.update_tenant(suspended)
    assert await store.get_tenant("acme") == suspended
    with pytest.raises(NotFoundError):
        await store.update_tenant(Tenant(tenant_id="ghost", name="x"))


async def test_api_keys(store: ControlPlaneStore) -> None:
    """凭据按租户列出；吊销只对本租户的有效凭据生效，且只生效一次。"""
    await _tenants(store, "acme", "other")
    first = ApiKeyRecord("aaaaaaaaaaaa", "acme", "ci", "tenant_admin", "h1", 1.0)
    second = ApiKeyRecord("bbbbbbbbbbbb", "acme", "app", "tenant_member", "h2", 2.0)
    foreign = ApiKeyRecord("cccccccccccc", "other", "app", "tenant_member", "h3", 3.0)
    for record in (first, second, foreign):
        await store.create_api_key(record)
    with pytest.raises(ConflictError):
        await store.create_api_key(first)

    assert await store.get_api_key("aaaaaaaaaaaa") == first
    assert await store.get_api_key("missing") is None
    assert await store.list_api_keys("acme") == [first, second]

    assert await store.revoke_api_key("other", "aaaaaaaaaaaa", at=9.0) is False
    assert await store.revoke_api_key("acme", "aaaaaaaaaaaa", at=9.0) is True
    assert await store.revoke_api_key("acme", "aaaaaaaaaaaa", at=10.0) is False
    revoked = await store.get_api_key("aaaaaaaaaaaa")
    assert revoked is not None
    assert (revoked.active, revoked.revoked_at) == (False, 9.0)


async def test_api_key_can_act_as_user(store: ControlPlaneStore) -> None:
    """代员工调用的能力原样存取；不给就是没有。"""
    await _tenants(store, "acme")
    plain = ApiKeyRecord("aaaaaaaaaaaa", "acme", "app", "tenant_member", "h1", 1.0)
    acting = ApiKeyRecord(
        "bbbbbbbbbbbb", "acme", "bot", "tenant_admin", "h2", 2.0, can_act_as_user=True
    )
    await store.create_api_key(plain)
    await store.create_api_key(acting)
    assert plain.can_act_as_user is False
    assert await store.get_api_key("aaaaaaaaaaaa") == plain
    assert await store.get_api_key("bbbbbbbbbbbb") == acting
    assert [key.can_act_as_user for key in await store.list_api_keys("acme")] == [False, True]


# --- 业务包 ---


async def test_package_versions_are_immutable_and_tenant_scoped(store: ControlPlaneStore) -> None:
    """已发布的版本不能覆盖；不同租户可以各有同名同版本的包，互不可见。"""
    await _tenants(store, "acme", "other")
    await store.add_package_version(_package("acme", "support", "1.0.0", at=1.0))
    await store.add_package_version(_package("acme", "support", "1.1.0", at=2.0))
    await store.add_package_version(_package("acme", "billing", "0.1.0", at=3.0))
    await store.add_package_version(_package("other", "support", "1.0.0", at=4.0))
    with pytest.raises(ConflictError):
        await store.add_package_version(_package("acme", "support", "1.0.0", at=5.0))

    found = await store.get_package_version("acme", "support", "1.0.0")
    assert found == _package("acme", "support", "1.0.0", at=1.0)
    assert await store.get_package_version("other", "support", "1.1.0") is None

    names = [(r.name, r.version) for r in await store.list_package_versions("acme")]
    assert names == [("support", "1.0.0"), ("support", "1.1.0"), ("billing", "0.1.0")]
    only = await store.list_package_versions("acme", "support")
    assert [r.version for r in only] == ["1.0.0", "1.1.0"]
    assert len(await store.list_package_versions("other")) == 1


async def test_active_version_switch_and_rollback(store: ControlPlaneStore) -> None:
    """生效版本可以前进也可以回滚；只能指向已发布的版本。"""
    await _tenants(store, "acme", "other")
    await store.add_package_version(_package("acme", "support", "1.0.0"))
    await store.add_package_version(_package("acme", "support", "1.1.0"))
    await store.add_package_version(_package("other", "support", "9.9.9"))
    assert await store.get_active_version("acme", "support") is None

    await store.set_active_version("acme", "support", "1.1.0", at=1.0)
    assert await store.get_active_version("acme", "support") == "1.1.0"
    await store.set_active_version("acme", "support", "1.0.0", at=2.0)
    assert await store.get_active_version("acme", "support") == "1.0.0"

    with pytest.raises(NotFoundError):
        await store.set_active_version("acme", "support", "9.9.9", at=3.0)
    assert await store.get_active_version("acme", "support") == "1.0.0"
    assert await store.get_active_version("other", "support") is None


# --- 密钥 ---


async def test_secrets(store: ControlPlaneStore) -> None:
    """密文原样存取（含任意字节）；覆盖、列出、删除都限定在租户内。"""
    await _tenants(store, "acme", "other")
    blob = bytes(range(256))
    await store.put_secret(SecretRecord("acme", "crm-token", blob, 1.0, "key:a"))
    await store.put_secret(SecretRecord("acme", "api-key", b"v1", 1.0, "key:a"))
    await store.put_secret(SecretRecord("other", "crm-token", b"theirs", 1.0, "key:o"))

    found = await store.get_secret("acme", "crm-token")
    assert found is not None
    assert found.ciphertext == blob
    assert await store.get_secret("acme", "missing") is None

    await store.put_secret(SecretRecord("acme", "api-key", b"v2", 2.0, "key:b"))
    updated = await store.get_secret("acme", "api-key")
    assert updated is not None
    assert (updated.ciphertext, updated.updated_at, updated.updated_by) == (b"v2", 2.0, "key:b")

    assert await store.list_secret_names("acme") == ["api-key", "crm-token"]
    assert await store.delete_secret("acme", "crm-token") is True
    assert await store.delete_secret("acme", "crm-token") is False
    theirs = await store.get_secret("other", "crm-token")
    assert theirs is not None
    assert theirs.ciphertext == b"theirs"


# --- 会话 ---


async def test_session_lifecycle(store: ControlPlaneStore) -> None:
    """建、取、更新、按状态列出、计数。"""
    await _tenants(store, "acme", "other")
    await store.create_session(_session("acme", "s1", at=1.0))
    await store.create_session(_session("acme", "s2", at=2.0))
    await store.create_session(_session("other", "s1", at=3.0))
    with pytest.raises(ConflictError):
        await store.create_session(_session("acme", "s1"))

    assert await store.get_session("acme", "s1") == _session("acme", "s1", at=1.0)
    assert await store.get_session("acme", "missing") is None

    updated = await store.update_session("acme", "s1", at=5.0, thread_id="thr_1")
    assert (updated.thread_id, updated.status, updated.updated_at) == ("thr_1", "active", 5.0)
    closed = await store.update_session("acme", "s2", at=6.0, status="closed")
    assert (closed.status, closed.thread_id) == ("closed", None)
    kept = await store.update_session("acme", "s1", at=7.0)
    assert kept.thread_id == "thr_1"
    with pytest.raises(NotFoundError):
        await store.update_session("acme", "ghost", at=1.0)

    assert [s.session_id for s in await store.list_sessions("acme")] == ["s1", "s2"]
    assert [s.session_id for s in await store.list_sessions("acme", status="active")] == ["s1"]
    assert [s.session_id for s in await store.list_sessions("acme", created_by="key:abc")] == [
        "s1",
        "s2",
    ]
    assert await store.list_sessions("acme", created_by="key:other") == []

    labeled = _session(
        "acme", "s3", at=3.0, metadata={"customer_id": "C-42", "备注": "中文 '引号'"}
    )
    await store.create_session(labeled)
    assert await store.get_session("acme", "s3") == labeled
    assert [s.session_id for s in await store.list_sessions("acme", limit=1)] == ["s1"]
    assert await store.count_active_sessions("acme") == 2
    assert await store.count_active_sessions("other") == 1
    assert await store.count_active_sessions("nobody") == 0


async def test_session_owner_user(store: ControlPlaneStore) -> None:
    """会话记下属于哪名员工（API key 直接建的为空），可按员工列出；更新不丢这个字段。"""
    await _tenants(store, "acme", "other")
    await store.create_session(_session("acme", "s1", at=1.0, user_id="usr_a"))
    await store.create_session(_session("acme", "s2", at=2.0, user_id="usr_b"))
    await store.create_session(_session("acme", "s3", at=3.0, user_id="usr_a"))
    await store.create_session(_session("acme", "s4", at=4.0))
    await store.create_session(_session("other", "s5", at=5.0, user_id="usr_a"))

    assert await store.get_session("acme", "s1") == _session("acme", "s1", at=1.0, user_id="usr_a")
    keyed = await store.get_session("acme", "s4")
    assert keyed is not None
    assert keyed.user_id is None

    mine = await store.list_sessions("acme", user_id="usr_a")
    assert [s.session_id for s in mine] == ["s3", "s1"]
    combined = await store.list_sessions("acme", user_id="usr_a", created_by="key:abc", limit=1)
    assert [s.session_id for s in combined] == ["s3"]
    assert [s.session_id for s in await store.list_sessions("other", user_id="usr_a")] == ["s5"]
    assert await store.list_sessions("acme", user_id="usr_missing") == []

    closed = await store.update_session("acme", "s1", at=9.0, status="closed")
    assert closed.user_id == "usr_a"
    leased = await store.acquire_lease("acme", "s3", owner="w1", now=10.0, ttl_seconds=30)
    assert leased.user_id == "usr_a"


async def test_session_of_another_tenant_is_invisible(store: ControlPlaneStore) -> None:
    """用别的租户的身份，拿着正确的会话标识也取不到、改不了、占不了。"""
    await _tenants(store, "acme", "other")
    await store.create_session(_session("acme", "s1"))
    assert await store.get_session("other", "s1") is None
    with pytest.raises(NotFoundError):
        await store.update_session("other", "s1", at=2.0, status="closed")
    with pytest.raises(NotFoundError):
        await store.acquire_lease("other", "s1", owner="w1", now=1.0, ttl_seconds=30)
    assert await store.release_lease("other", "s1", owner="w1") is False
    untouched = await store.get_session("acme", "s1")
    assert untouched is not None
    assert (untouched.status, untouched.owner) == ("active", None)


async def test_lease_single_holder(store: ControlPlaneStore) -> None:
    """持有权：同一时刻只有一个持有者；自己可以续期；到期后别人可以接手。"""
    await _tenants(store, "acme")
    await store.create_session(_session("acme", "s1"))

    held = await store.acquire_lease("acme", "s1", owner="w1", now=100.0, ttl_seconds=30)
    assert (held.owner, held.lease_expires_at) == ("w1", 130.0)

    with pytest.raises(SessionBusyError) as caught:
        await store.acquire_lease("acme", "s1", owner="w2", now=110.0, ttl_seconds=30)
    assert caught.value.owner == "w1"

    renewed = await store.acquire_lease("acme", "s1", owner="w1", now=120.0, ttl_seconds=30)
    assert renewed.lease_expires_at == 150.0

    taken = await store.acquire_lease("acme", "s1", owner="w2", now=150.0, ttl_seconds=30)
    assert (taken.owner, taken.lease_expires_at) == ("w2", 180.0)


async def test_lease_release(store: ControlPlaneStore) -> None:
    """只有持有者能放弃；放弃后别人立刻可以取得。"""
    await _tenants(store, "acme")
    await store.create_session(_session("acme", "s1"))
    await store.acquire_lease("acme", "s1", owner="w1", now=100.0, ttl_seconds=300)
    assert await store.release_lease("acme", "s1", owner="w2") is False
    assert await store.release_lease("acme", "s1", owner="w1") is True
    assert await store.release_lease("acme", "s1", owner="w1") is False
    taken = await store.acquire_lease("acme", "s1", owner="w2", now=101.0, ttl_seconds=30)
    assert taken.owner == "w2"


async def test_lease_race_has_exactly_one_winner(store: ControlPlaneStore) -> None:
    """多个实例同时抢同一个会话：恰好一个成功。"""
    await _tenants(store, "acme")
    await store.create_session(_session("acme", "s1"))

    async def attempt(owner: str) -> str | None:
        try:
            await store.acquire_lease("acme", "s1", owner=owner, now=100.0, ttl_seconds=30)
        except SessionBusyError:
            return None
        return owner

    winners = [w for w in await asyncio.gather(*(attempt(f"w{i}") for i in range(12))) if w]
    assert len(winners) == 1
    final = await store.get_session("acme", "s1")
    assert final is not None
    assert final.owner == winners[0]


# --- 用量 ---


async def test_usage_accumulates_per_tenant_per_day(store: ControlPlaneStore) -> None:
    """用量按租户、按天累加。"""
    await _tenants(store, "acme", "other")
    empty = await store.get_usage("acme", "2026-09-30")
    assert (empty.total_tokens, empty.turns) == (0, 0)

    await store.add_usage(
        "acme", "2026-09-30", input_tokens=100, output_tokens=20, total_tokens=120, turns=1
    )
    total = await store.add_usage(
        "acme", "2026-09-30", input_tokens=50, output_tokens=5, total_tokens=55, turns=1
    )
    assert (total.input_tokens, total.output_tokens, total.total_tokens, total.turns) == (
        150, 25, 175, 2,
    )  # fmt: skip
    await store.add_usage(
        "acme", "2026-10-01", input_tokens=1, output_tokens=1, total_tokens=2, turns=1
    )
    await store.add_usage(
        "other", "2026-09-30", input_tokens=9, output_tokens=9, total_tokens=18, turns=3
    )

    assert await store.get_usage("acme", "2026-09-30") == total
    assert (await store.get_usage("other", "2026-09-30")).total_tokens == 18
    days = await store.list_usage("acme", since="2026-09-30", until="2026-10-01")
    assert [(r.day, r.total_tokens) for r in days] == [("2026-09-30", 175), ("2026-10-01", 2)]
    assert await store.list_usage("acme", since="2026-10-02", until="2026-10-31") == []


async def test_concurrent_usage_is_not_lost(store: ControlPlaneStore) -> None:
    """并发累加不丢数。"""
    await _tenants(store, "acme")
    await asyncio.gather(
        *(
            store.add_usage(
                "acme", "2026-09-30", input_tokens=3, output_tokens=2, total_tokens=5, turns=1
            )
            for _ in range(40)
        )
    )
    total = await store.get_usage("acme", "2026-09-30")
    assert (total.input_tokens, total.output_tokens, total.total_tokens, total.turns) == (
        120, 80, 200, 40,
    )  # fmt: skip


# --- 审计 ---


async def test_audit_append_assigns_increasing_ids(store: ControlPlaneStore) -> None:
    """追加后按标识升序取回，内容原样保留。"""
    await _tenants(store, "acme")
    assert await store.append_audit([]) == 0
    records = [_engine_event("acme", seq) for seq in range(3)]
    assert await store.append_audit(records) == 3
    found = await store.query_audit("acme", AuditQuery())
    assert [r.seq for r in found] == [0, 1, 2]
    assert [r.audit_id for r in found] == sorted(r.audit_id for r in found)
    assert found[0].audit_id > 0
    assert found[1].data == {"n": 1, "文本": "引号 ' \" 都要原样保存"}
    assert (found[1].source, found[1].actor, found[1].at) == ("engine", "engine", 101.0)


async def test_audit_engine_events_are_deduplicated(store: ControlPlaneStore) -> None:
    """同一事件流里同一序号的引擎事件只保留先到的；不同事件流互不影响。"""
    await _tenants(store, "acme")
    await store.append_audit([_engine_event("acme", 0), _engine_event("acme", 1)])
    written = await store.append_audit(
        [
            _engine_event("acme", 1),
            _engine_event("acme", 2),
            _engine_event("acme", 0, stream="st-2"),
        ]
    )
    assert written == 2
    found = await store.query_audit("acme", AuditQuery())
    assert [(r.stream, r.seq) for r in found] == [
        ("st-1", 0), ("st-1", 1), ("st-1", 2), ("st-2", 0),
    ]  # fmt: skip


async def test_audit_non_engine_records_are_never_deduplicated(store: ControlPlaneStore) -> None:
    """闸门与控制面的记录没有序号，内容相同也各算一条。"""
    await _tenants(store, "acme")
    record = AuditRecord(
        tenant_id="acme", source="gate", kind="connector_call", at=1.0, actor="key:a"
    )
    assert await store.append_audit([record, record]) == 2
    assert await store.append_audit([record]) == 1
    assert len(await store.query_audit("acme", AuditQuery())) == 3


async def test_audit_is_tenant_scoped(store: ControlPlaneStore) -> None:
    """审计记录按租户隔离，同一事件流序号在不同租户下互不去重。"""
    await _tenants(store, "acme", "other")
    await store.append_audit([_engine_event("acme", 0), _engine_event("other", 0)])
    assert [r.tenant_id for r in await store.query_audit("acme", AuditQuery())] == ["acme"]
    assert [r.tenant_id for r in await store.query_audit("other", AuditQuery())] == ["other"]
    assert await store.query_audit("nobody", AuditQuery()) == []


async def test_audit_query_filters_and_pagination(store: ControlPlaneStore) -> None:
    """过滤条件是 AND 关系；按标识翻页不重不漏。"""
    await _tenants(store, "acme")
    records = [_engine_event("acme", seq, session="s1" if seq % 2 else "s2") for seq in range(10)]
    records.append(
        AuditRecord(
            tenant_id="acme",
            source="control",
            kind="package_published",
            at=500.0,
            actor="key:a",
            data={"name": "support"},
        )
    )
    await store.append_audit(records)

    by_session = await store.query_audit("acme", AuditQuery(session_id="s1"))
    assert [r.seq for r in by_session] == [1, 3, 5, 7, 9]
    by_kind = await store.query_audit("acme", AuditQuery(kind="package_published"))
    assert [r.data for r in by_kind] == [{"name": "support"}]
    by_source = await store.query_audit("acme", AuditQuery(source="control"))
    assert len(by_source) == 1
    window = await store.query_audit("acme", AuditQuery(since=103.0, until=106.0))
    assert [r.seq for r in window] == [3, 4, 5]
    combined = await store.query_audit(
        "acme", AuditQuery(session_id="s1", since=103.0, until=106.0, source="engine")
    )
    assert [r.seq for r in combined] == [3, 5]
    seen: list[int] = []
    after = 0
    for _ in range(10):
        page = await store.query_audit("acme", AuditQuery(after_id=after, limit=4))
        if not page:
            break
        seen.extend(r.audit_id for r in page)
        after = page[-1].audit_id
    assert len(seen) == 11
    assert seen == sorted(set(seen))


async def test_audit_query_by_submission(store: ControlPlaneStore) -> None:
    """按一轮对话过滤：看记录里的 ``submission_id``。"""
    await _tenants(store, "acme")
    await store.append_audit(
        [
            AuditRecord(
                tenant_id="acme",
                source="engine",
                kind="turn_text",
                at=200.0,
                actor="engine",
                session_id="s1",
                data={"submission_id": "sub_x", "text": "全文"},
            ),
            AuditRecord(
                tenant_id="acme",
                source="engine",
                kind="turn_text",
                at=201.0,
                actor="engine",
                session_id="s1",
                data={"submission_id": "sub_y", "text": "别的"},
            ),
        ]  # fmt: skip
    )
    by_submission = await store.query_audit("acme", AuditQuery(submission_id="sub_x"))
    assert [r.data["text"] for r in by_submission] == ["全文"]


async def test_audit_user_id_round_trip_and_filter(store: ControlPlaneStore) -> None:
    """审计记录带上员工：原样存取，没有时为空；可按员工过滤，且只在本租户内。"""
    await _tenants(store, "acme", "other")

    def control(tenant: str, kind: str, user_id: str | None) -> AuditRecord:
        return AuditRecord(
            tenant_id=tenant, source="control", kind=kind, at=1.0, actor="key:a", user_id=user_id
        )

    await store.append_audit(
        [
            control("acme", "login_succeeded", "usr_a"),
            control("acme", "package_published", None),
            control("acme", "logout", "usr_a"),
            control("acme", "login_succeeded", "usr_b"),
            control("other", "login_succeeded", "usr_a"),
            _engine_event("acme", 0),
        ]
    )
    everything = await store.query_audit("acme", AuditQuery())
    assert [r.user_id for r in everything] == ["usr_a", None, "usr_a", "usr_b", None]

    alice = await store.query_audit("acme", AuditQuery(user_id="usr_a"))
    assert [r.kind for r in alice] == ["login_succeeded", "logout"]
    both = await store.query_audit("acme", AuditQuery(user_id="usr_a", kind="logout"))
    assert [r.kind for r in both] == ["logout"]
    theirs = await store.query_audit("other", AuditQuery(user_id="usr_a"))
    assert [r.tenant_id for r in theirs] == ["other"]
    assert await store.query_audit("acme", AuditQuery(user_id="usr_missing")) == []


async def test_audit_query_by_actor(store: ControlPlaneStore) -> None:
    """按触发者过滤：精确匹配（代员工调用的 ``key:<id>/user:<id>`` 不算 ``key:<id>`` 的），可与员工
    条件组合，且只在本租户内。"""
    await _tenants(store, "acme", "other")

    def control(tenant: str, actor: str, user_id: str | None) -> AuditRecord:
        return AuditRecord(
            tenant_id=tenant, source="control", kind="message_sent", at=1.0, actor=actor,
            user_id=user_id,
        )  # fmt: skip

    await store.append_audit(
        [
            control("acme", "key:k1", "usr_a"),
            control("acme", "key:k1/user:usr_b", "usr_b"),
            control("acme", "user:usr_a", "usr_a"),
            control("acme", "key:k1", None),
            control("other", "key:k1", "usr_a"),
            _engine_event("acme", 0),
        ]
    )
    by_key = await store.query_audit("acme", AuditQuery(actor="key:k1"))
    assert [(r.actor, r.user_id) for r in by_key] == [("key:k1", "usr_a"), ("key:k1", None)]
    in_alice = await store.query_audit("acme", AuditQuery(actor="key:k1", user_id="usr_a"))
    assert [(r.actor, r.user_id) for r in in_alice] == [("key:k1", "usr_a")]
    acting = await store.query_audit("acme", AuditQuery(actor="key:k1/user:usr_b"))
    assert [r.user_id for r in acting] == ["usr_b"]
    assert [r.kind for r in await store.query_audit("acme", AuditQuery(actor="engine"))] == [
        _engine_event("acme", 0).kind
    ]
    theirs = await store.query_audit("other", AuditQuery(actor="key:k1"))
    assert [r.tenant_id for r in theirs] == ["other"]
    assert await store.query_audit("acme", AuditQuery(actor="key:nobody")) == []


@pytest.mark.parametrize("limit", [0, 1001])
def test_audit_query_limit_bounds(limit: int) -> None:
    """单页条数有上下限。"""
    with pytest.raises(ValueError, match="limit"):
        AuditQuery(limit=limit)
