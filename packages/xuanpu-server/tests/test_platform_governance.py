"""治理面：认证、租户隔离、业务包版本、密钥、配额、会话持有权。"""

from __future__ import annotations

import asyncio
import io
import zipfile
from typing import TYPE_CHECKING, Any

import pytest
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PackageInvalidError,
    PermissionDeniedError,
    QuotaExceededError,
    SecretMissingError,
    SessionBusyError,
)
from xuanpu_server.platform import ROOT_PRINCIPAL, Platform
from xuanpu_server.runtime import LocalSandboxProvider
from xuanpu_server.secrets import (
    EncryptedVault,
    SecretCorruptedError,
    generate_master_key,
)
from xuanpu_server.tenancy import QuotaLimits

if TYPE_CHECKING:
    from xuanpu_server.tenancy import Principal


async def second_tenant(world: Any) -> Principal:
    """再建一个租户，返回它的管理员身份。"""
    await world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="globex", name="Globex")
    issued = await world.platform.issue_key(
        ROOT_PRINCIPAL, tenant_id="globex", name="admin", role="tenant_admin"
    )
    return await world.platform.authenticate(issued.key)


# --- 认证 ---


async def test_authentication(world: Any) -> None:
    """根凭据、租户凭据各得其身份；各种无效凭据得到同一个提示。"""
    root = await world.platform.authenticate(world.root_key)
    assert (root.role, root.tenant_id) == ("platform_admin", None)
    assert (world.admin.role, world.admin.tenant_id) == ("tenant_admin", "acme")
    assert (world.member.role, world.member.tenant_id) == ("tenant_member", "acme")

    tampered = world.member_key[:-1] + ("A" if world.member_key[-1] != "A" else "B")
    wrong_id = "xp_" + "0" * 12 + "_" + world.member_key.rsplit("_", 1)[1]
    messages = set()
    for presented in ("", "garbage", "xp_short_x", tampered, wrong_id, "xproot_wrong"):
        with pytest.raises(AuthenticationError) as caught:
            await world.platform.authenticate(presented)
        messages.add(caught.value.message)
    assert messages == {"凭据无效"}


async def test_revoked_key_and_suspended_tenant_cannot_authenticate(world: Any) -> None:
    """吊销的凭据立即失效；租户停用后它的全部凭据失效，恢复后重新可用。"""
    assert await world.platform.revoke_key(world.admin, world.member.key_id) is True
    with pytest.raises(AuthenticationError):
        await world.platform.authenticate(world.member_key)
    assert await world.platform.revoke_key(world.admin, world.member.key_id) is False

    await world.platform.set_tenant_status(ROOT_PRINCIPAL, "acme", "suspended")
    with pytest.raises(AuthenticationError):
        await world.platform.authenticate(world.admin_key)
    await world.platform.set_tenant_status(ROOT_PRINCIPAL, "acme", "active")
    assert (await world.platform.authenticate(world.admin_key)).tenant_id == "acme"


async def test_role_boundaries(world: Any) -> None:
    """成员不能做管理员的事；租户管理员不能做平台管理员的事；根凭据不能直接进租户。"""
    with pytest.raises(PermissionDeniedError):
        await world.platform.packages.publish(world.member, world.archive())
    with pytest.raises(PermissionDeniedError):
        await world.platform.put_secret(world.member, "x", "y")
    with pytest.raises(PermissionDeniedError):
        await world.platform.issue_key(world.member, tenant_id=None, name="x", role="tenant_member")
    with pytest.raises(PermissionDeniedError):
        await world.platform.create_tenant(world.admin, tenant_id="evil-corp", name="x")
    with pytest.raises(PermissionDeniedError):
        await world.platform.set_tenant_status(world.admin, "acme", "suspended")
    with pytest.raises(PermissionDeniedError):
        await world.platform.sessions.create(ROOT_PRINCIPAL, package="support-desk")
    with pytest.raises(ConflictError):
        await world.platform.issue_key(
            ROOT_PRINCIPAL, tenant_id="acme", name="x", role="platform_admin"
        )


async def test_tenant_admin_issues_keys_only_in_own_tenant(world: Any) -> None:
    """租户管理员能给自己的租户签发凭据，给别的租户签发按不存在处理。"""
    await second_tenant(world)
    issued = await world.platform.issue_key(
        world.admin, tenant_id=None, name="ci", role="tenant_member"
    )
    assert (await world.platform.authenticate(issued.key)).tenant_id == "acme"
    with pytest.raises(NotFoundError):
        await world.platform.issue_key(
            world.admin, tenant_id="globex", name="ci", role="tenant_member"
        )


@pytest.mark.parametrize("tenant_id", ["", "A", "ab", "Acme", "a_b_c", "../etc", "a" * 41, "-ab"])
async def test_invalid_tenant_id_rejected(world: Any, tenant_id: str) -> None:
    """租户标识会进目录名，字符集受限。"""
    with pytest.raises(ValueError, match="租户标识"):
        await world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id=tenant_id, name="x")


# --- 租户隔离 ---


async def test_tenants_cannot_see_each_other(world: Any, step: Any, read_turn: Any) -> None:
    """另一个租户看不到 acme 的业务包、会话、密钥、审计、用量。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="hi"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    outsider = await second_tenant(world)

    with pytest.raises(NotFoundError):
        await world.platform.sessions.create(outsider, package="support-desk")
    with pytest.raises(NotFoundError):
        await world.platform.sessions.get(outsider, session.session_id)
    with pytest.raises(NotFoundError):
        await world.platform.sessions.send_message(outsider, session.session_id, "窃听")
    with pytest.raises(NotFoundError):
        await world.platform.sessions.resume(outsider, session.session_id, {"r": {}})
    with pytest.raises(NotFoundError):
        await world.platform.sessions.close(outsider, session.session_id)
    assert await world.platform.sessions.list_sessions(outsider) == []
    assert await world.platform.packages.list_versions("globex") == []
    assert await world.platform.vault.names("globex") == []
    with pytest.raises(SecretMissingError):
        await world.platform.vault.get("globex", "crm-token")

    theirs = await world.platform.store.query_audit("globex", AuditQuery(limit=1000))
    assert {record.kind for record in theirs} == {"tenant_created", "api_key_issued"}
    day = (await world.platform.store.list_usage("acme", since="0000", until="9999"))[0].day
    assert (await world.platform.store.get_usage("globex", day)).total_tokens == 0


async def test_same_package_name_in_two_tenants(world: Any, step: Any, read_turn: Any) -> None:
    """两个租户各自发布同名同版本的业务包：文件、运行时、会话存储各在各的目录。"""
    await world.publish()
    outsider = await second_tenant(world)
    await world.platform.put_secret(outsider, "crm-token", "globex-token")
    await world.platform.packages.publish(outsider, world.archive(), activate=True)

    ours = await world.platform.sessions.create(world.member, package="support-desk")
    theirs = await world.platform.sessions.create(outsider, package="support-desk")
    world.models.script(step(text="a"), step(text="b"))
    await read_turn(await world.platform.sessions.send_message(world.member, ours.session_id, "1"))
    await read_turn(await world.platform.sessions.send_message(outsider, theirs.session_id, "2"))

    assert sorted(world.models.created_for) == [("acme", "balanced"), ("globex", "balanced")]
    first = await world.platform.packages.resolve("acme", "support-desk")
    second = await world.platform.packages.resolve("globex", "support-desk")
    assert first.root != second.root
    cache = world.data_dir / "cache" / "packages"
    assert first.root.relative_to(cache).parts[0].startswith("acme-")
    assert second.root.relative_to(cache).parts[0].startswith("globex-")
    hexdigest = first.record.digest.removeprefix("sha256:")
    assert first.record.location == f"tenants/acme/packages/support-desk/1.0.0/{hexdigest}.zip"
    assert second.record.location == f"tenants/globex/packages/support-desk/1.0.0/{hexdigest}.zip"
    thread_dirs = sorted(path.name for path in (world.data_dir / "threads").iterdir())
    assert len(thread_dirs) == 2
    assert thread_dirs[0].startswith("acme-")
    assert thread_dirs[1].startswith("globex-")


# --- 业务包 ---


async def test_publish_is_immutable_and_versions_switch(world: Any) -> None:
    """同一版本不能重复发布；生效版本可以前进和回滚；新会话跟随生效版本。"""
    await world.platform.packages.publish(world.admin, world.archive(version="1.0.0"))
    with pytest.raises(NotFoundError):
        await world.platform.sessions.create(world.member, package="support-desk")
    with pytest.raises(ConflictError):
        await world.platform.packages.publish(world.admin, world.archive(version="1.0.0"))

    await world.platform.packages.activate(world.admin, "support-desk", "1.0.0")
    await world.platform.packages.publish(
        world.admin, world.archive(version="1.1.0"), activate=True
    )
    await world.platform.packages.publish(world.admin, world.archive(version="1.10.0"))
    versions = await world.platform.packages.list_versions("acme", "support-desk")
    assert [record.version for record in versions] == ["1.0.0", "1.1.0", "1.10.0"]

    newer = await world.platform.sessions.create(world.member, package="support-desk")
    assert newer.version == "1.1.0"
    await world.platform.packages.activate(world.admin, "support-desk", "1.0.0")
    rolled_back = await world.platform.sessions.create(world.member, package="support-desk")
    assert rolled_back.version == "1.0.0"
    pinned = await world.platform.sessions.create(
        world.member, package="support-desk", version="1.10.0"
    )
    assert pinned.version == "1.10.0"
    # 已有的会话不受切换影响
    assert (await world.platform.sessions.get(world.member, newer.session_id)).version == "1.1.0"

    with pytest.raises(NotFoundError):
        await world.platform.packages.activate(world.admin, "support-desk", "9.9.9")
    with pytest.raises(NotFoundError):
        await world.platform.sessions.create(world.member, package="support-desk", version="9.9.9")
    with pytest.raises(InvalidRequestError, match="不是业务包"):
        await world.platform.sessions.create(world.member, package="support-desk", entry="faq")


def _archive(files: dict[str, str]) -> bytes:
    """手工打一个归档。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return buffer.getvalue()


MINIMAL_MANIFEST = (
    "apiVersion: xuanpu/v1\n"
    "metadata: {name: tiny, version: 1.0.0}\n"
    "spec: {skills: {entries: [main]}}\n"
)
MAIN_SKILL = (
    "---\nname: main\ndescription: d\nversion: 1.0.0\ntype: composite\nentry: true\n"
    "child_skills: [helper]\n---\n正文\n"
)
HELPER_SKILL = "---\nname: helper\ndescription: d\nversion: 1.0.0\ntype: atomic\n---\n正文\n"


@pytest.mark.parametrize(
    ("files", "fragment"),
    [
        ({"readme.txt": "x"}, "归档不合法"),
        ({"xuanpu.yaml": "apiVersion: xuanpu/v9\n"}, "apiVersion"),
        ({"xuanpu.yaml": MINIMAL_MANIFEST}, "不存在或为空"),
        (
            {"xuanpu.yaml": MINIMAL_MANIFEST, "skills/helper/SKILL.md": HELPER_SKILL},
            "找不到",
        ),
        (
            {
                "xuanpu.yaml": MINIMAL_MANIFEST,
                "skills/main/SKILL.md": MAIN_SKILL,
                "../escape.txt": "x",
            },
            "归档不合法",
        ),
        (
            # 规范校验能过、但内核不认的 skill：composite 引用了不存在的子 skill
            {"xuanpu.yaml": MINIMAL_MANIFEST, "skills/main/SKILL.md": MAIN_SKILL},
            "skill 加载失败",
        ),
    ],
)
async def test_invalid_packages_are_rejected_and_leave_nothing(
    world: Any, files: dict[str, str], fragment: str
) -> None:
    """不合格的业务包发布失败，数据目录与对象存储里都不留下任何东西。"""
    with pytest.raises(PackageInvalidError) as caught:
        await world.platform.packages.publish(world.admin, _archive(files))
    assert fragment in caught.value.message + "".join(caught.value.problems)
    assert await world.platform.packages.list_versions("acme") == []
    # world 夹具的对象存储放在数据目录旁边的 objects/
    leftovers = [
        path
        for root in (world.data_dir / "cache", world.data_dir.parent / "objects")
        for path in (root.rglob("*") if root.exists() else ())
        if path.is_file() or (path.is_dir() and path.name != ".staging")
    ]
    assert leftovers == []
    assert not (world.data_dir.parent / "escape.txt").exists()


async def test_valid_minimal_package_publishes(world: Any) -> None:
    """最小的合法业务包可以发布。"""
    record = await world.platform.packages.publish(
        world.admin,
        _archive(
            {
                "xuanpu.yaml": MINIMAL_MANIFEST,
                "skills/main/SKILL.md": MAIN_SKILL,
                "skills/helper/SKILL.md": HELPER_SKILL,
            }
        ),
    )
    assert (record.name, record.version, record.published_by) == (
        "tiny", "1.0.0", world.admin.actor,
    )  # fmt: skip
    assert record.digest.startswith("sha256:")


# --- 密钥 ---


async def test_missing_secret_blocks_the_session(world: Any, step: Any) -> None:
    """业务包引用的密钥没配：对话起不来，并且说清楚缺哪条。"""
    await world.platform.delete_secret(world.admin, "crm-token")
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    with pytest.raises(SecretMissingError, match="crm-token"):
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    # 失败不占着会话：配好密钥之后可以继续
    stored = await world.platform.sessions.get(world.member, session.session_id)
    assert stored.owner is None
    await world.platform.put_secret(world.admin, "crm-token", "now-configured")
    world.models.script(step(text="好了"))
    await world.platform.sessions.send_message(world.member, session.session_id, "你好")


async def test_secret_values_never_reach_audit_or_listing(world: Any) -> None:
    """密钥取值不出现在名单、审计里；存储里只有密文。"""
    await world.platform.put_secret(world.admin, "api-key", "plain-text-value")
    assert await world.platform.vault.names("acme") == ["api-key", "crm-token"]
    record = await world.platform.store.get_secret("acme", "api-key")
    assert record is not None
    assert b"plain-text-value" not in record.ciphertext
    audit = await world.platform.store.query_audit("acme", AuditQuery(kind="secret_written"))
    assert [item.data for item in audit] == [{"name": "crm-token"}, {"name": "api-key"}]


@pytest.mark.parametrize("name", ["", "Upper", "has space", "../x", "a" * 64])
async def test_invalid_secret_names(world: Any, name: str) -> None:
    """密钥名受字符集约束。"""
    with pytest.raises(InvalidRequestError):
        await world.platform.put_secret(world.admin, name, "v")


async def test_secret_ciphertext_is_bound_to_tenant_and_name(world: Any) -> None:
    """密文被挪到别的租户或别的名字下就解不开；被改动过也解不开。"""
    from xuanpu_server.controlplane import SecretRecord

    await world.platform.create_tenant(ROOT_PRINCIPAL, tenant_id="globex", name="Globex")
    original = await world.platform.store.get_secret("acme", "crm-token")
    assert original is not None
    for tenant, name in (("globex", "crm-token"), ("acme", "other-name")):
        await world.platform.store.put_secret(
            SecretRecord(tenant, name, original.ciphertext, 1.0, "attacker")
        )
        with pytest.raises(SecretCorruptedError):
            await world.platform.vault.get(tenant, name)
    flipped = bytearray(original.ciphertext)
    flipped[-1] ^= 0x01
    await world.platform.store.put_secret(
        SecretRecord("acme", "crm-token", bytes(flipped), 1.0, "attacker")
    )
    with pytest.raises(SecretCorruptedError):
        await world.platform.vault.get("acme", "crm-token")


async def test_master_key_rotation(world: Any) -> None:
    """轮换主密钥：新旧并存时旧密文仍可读，新写入用新钥匙；撤掉旧钥匙后旧密文读不了。"""
    store = world.platform.store
    old, new = generate_master_key(), generate_master_key()
    await EncryptedVault(store, keys={"old": old}, primary="old").put(
        "acme", "legacy", "v-old", actor="t"
    )
    both = EncryptedVault(store, keys={"old": old, "new": new}, primary="new")
    assert await both.get("acme", "legacy") == "v-old"
    await both.put("acme", "fresh", "v-new", actor="t")

    only_new = EncryptedVault(store, keys={"new": new}, primary="new")
    assert await only_new.get("acme", "fresh") == "v-new"
    with pytest.raises(SecretCorruptedError, match="已不在配置里"):
        await only_new.get("acme", "legacy")

    with pytest.raises(ValueError, match="32 字节"):
        EncryptedVault(store, keys={"k": b"short"}, primary="k")
    with pytest.raises(ValueError, match="不在 keys"):
        EncryptedVault(store, keys={"k": new}, primary="missing")


# --- 配额 ---


async def test_session_quota(world: Any) -> None:
    """活动会话数到上限后不能再建；关掉一个就又能建。"""
    await world.publish()
    tenant = await world.platform.store.get_tenant("acme")
    assert tenant is not None
    from dataclasses import replace

    await world.platform.store.update_tenant(
        replace(tenant, quota=QuotaLimits(max_active_sessions=2))
    )
    first = await world.platform.sessions.create(world.member, package="support-desk")
    await world.platform.sessions.create(world.member, package="support-desk")
    with pytest.raises(QuotaExceededError, match="活动会话数"):
        await world.platform.sessions.create(world.member, package="support-desk")
    await world.platform.sessions.close(world.member, first.session_id)
    await world.platform.sessions.create(world.member, package="support-desk")


async def test_daily_token_quota(world: Any, step: Any, read_turn: Any) -> None:
    """当天用量到上限后不能再开新的一轮对话。"""
    await world.publish()
    tenant = await world.platform.store.get_tenant("acme")
    assert tenant is not None
    from dataclasses import replace

    await world.platform.store.update_tenant(
        replace(tenant, quota=QuotaLimits(max_daily_tokens=200))
    )
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="一"), step(text="二"), step(text="三"))
    for text in ("1", "2"):
        await read_turn(
            await world.platform.sessions.send_message(world.member, session.session_id, text)
        )
    with pytest.raises(QuotaExceededError, match="token"):
        await world.platform.sessions.send_message(world.member, session.session_id, "3")
    assert len(world.models.requests) == 2


# --- 会话 ---


@pytest.mark.parametrize("text", ["", "   \n", "x" * 200_001])
async def test_invalid_messages(world: Any, text: str) -> None:
    """空消息与超长消息被拒绝。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    with pytest.raises(InvalidRequestError):
        await world.platform.sessions.send_message(world.member, session.session_id, text)


async def test_closed_session_rejects_messages(world: Any, step: Any, read_turn: Any) -> None:
    """关闭后的会话不能再对话；重复关闭无害。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="hi"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    closed = await world.platform.sessions.close(world.member, session.session_id)
    assert (closed.status, closed.owner) == ("closed", None)
    assert (
        await world.platform.sessions.close(world.member, session.session_id)
    ).status == "closed"
    with pytest.raises(ConflictError, match="已关闭"):
        await world.platform.sessions.send_message(world.member, session.session_id, "还在吗")
    assert [
        s.session_id
        for s in await world.platform.sessions.list_sessions(world.member, status="active")
    ] == []


async def test_concurrent_sessions_do_not_mix(world: Any, step: Any, read_turn: Any) -> None:
    """多个会话并发对话：各自的事件流只有自己的事件，用量与审计都齐。"""
    await world.publish()
    sessions = [
        await world.platform.sessions.create(world.member, package="support-desk")
        for _ in range(12)
    ]
    world.models.script(*(step(text=f"回复-{index}") for index in range(12)))
    turns = await asyncio.gather(
        *(
            world.platform.sessions.send_message(world.member, item.session_id, f"问题-{index}")
            for index, item in enumerate(sessions)
        )
    )
    streams = await asyncio.gather(*(read_turn(turn) for turn in turns))
    for turn, events in zip(turns, streams, strict=True):
        assert {event.submission_id for event in events} == {turn.submission_id}
        assert events[-1].msg.kind == "turn_completed"
    replies = sorted(
        "".join(e.msg.data.get("delta", "") for e in events if e.msg.kind == "assistant_text")
        for events in streams
    )
    assert replies == sorted(f"回复-{index}" for index in range(12))

    day = (await world.platform.store.list_usage("acme", since="0000", until="9999"))[0]
    assert (day.turns, day.total_tokens) == (12, 12 * 120)
    completed = await world.platform.store.query_audit(
        "acme", AuditQuery(kind="turn_completed", limit=1000)
    )
    assert sorted(record.session_id for record in completed) == sorted(
        item.session_id for item in sessions
    )


def _another_instance(world: Any, instance_id: str, **kwargs: Any) -> Platform:
    """同一份存储、对象存储与数据目录上的另一个平台实例。"""
    return Platform(
        store=world.platform.store,
        vault=world.platform.vault,
        models=world.models,
        sandboxes=LocalSandboxProvider(),
        data_dir=world.data_dir,
        root_key_hash="0" * 64,
        objects=world.objects,
        instance_id=instance_id,
        **kwargs,
    )


async def test_session_has_a_single_holder(world: Any, step: Any, read_turn: Any) -> None:
    """一个实例持有会话时，另一个实例不能同时对它写；提示里带上持有者。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="hi"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "你好")
    )
    other = _another_instance(world, "inst-other")
    try:
        with pytest.raises(SessionBusyError) as caught:
            await other.sessions.send_message(world.member, session.session_id, "抢")
        assert caught.value.owner == "inst-test"
    finally:
        await other.close()


async def test_another_instance_takes_over_after_release(
    world: Any, step: Any, read_turn: Any
) -> None:
    """实例退出后，另一个实例从共用的会话存储里续接：上下文还在，审计接得上。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    world.models.script(step(text="订单号是 A-1024"), step(text="是 A-1024"))
    await read_turn(
        await world.platform.sessions.send_message(world.member, session.session_id, "查订单")
    )
    thread_id = (await world.platform.sessions.get(world.member, session.session_id)).thread_id
    await world.platform.close()

    released = await world.platform.store.get_session("acme", session.session_id)
    assert released is not None
    assert (released.status, released.owner) == ("active", None)

    other = _another_instance(world, "inst-other")
    try:
        events = await read_turn(
            await other.sessions.send_message(world.member, session.session_id, "刚才的订单号？")
        )
        assert events[-1].msg.kind == "turn_completed"
        assert "订单号是 A-1024" in world.models.last_prompt()
        taken = await other.sessions.get(world.member, session.session_id)
        assert (taken.owner, taken.thread_id) == ("inst-other", thread_id)
    finally:
        await other.close()

    completed = await world.platform.store.query_audit(
        "acme", AuditQuery(kind="turn_completed", session_id=session.session_id)
    )
    assert len(completed) == 2
    # 两个实例各是一条事件流，序号各自从头计，互不去重
    assert len({record.stream for record in completed}) == 2


async def test_expired_lease_can_be_taken_over(world: Any, step: Any) -> None:
    """持有者没来得及放手就没了（崩溃）：租约到期后别的实例可以接手，到期前不行。"""
    await world.publish()
    session = await world.platform.sessions.create(world.member, package="support-desk")
    clock = {"now": 1_000.0}
    crashed = _another_instance(world, "inst-crashed", now=lambda: clock["now"], lease_seconds=60)
    world.models.script(step(text="一"), step(text="二"))
    await crashed.sessions.send_message(world.member, session.session_id, "1")

    survivor = _another_instance(world, "inst-survivor", now=lambda: clock["now"], lease_seconds=60)
    try:
        clock["now"] = 1_059.0
        with pytest.raises(SessionBusyError):
            await survivor.sessions.send_message(world.member, session.session_id, "2")
        clock["now"] = 1_061.0
        await survivor.sessions.send_message(world.member, session.session_id, "2")
        taken = await survivor.sessions.get(world.member, session.session_id)
        assert taken.owner == "inst-survivor"
    finally:
        await survivor.close()
        await crashed.close()
