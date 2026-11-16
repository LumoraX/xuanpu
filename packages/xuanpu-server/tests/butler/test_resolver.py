"""匠人解析：本人可用的生效入口；未生效、未发布、停用员工、机器身份一律不可用。"""

from __future__ import annotations

import time
from typing import Any

import pytest
from xuanpu_server.errors import NotFoundError, PermissionDeniedError
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.config import DurableExecutionConfig


async def _alice(world: Any) -> Principal:
    """在职员工 Alice。"""
    await world.platform.identity.store.create_user(UserRecord(
        tenant_id="acme", user_id="usr_alice", display_name="Alice", created_at=1.0))
    return Principal(role="tenant_member", tenant_id="acme", key_id=None, user_id="usr_alice")


async def test_lists_and_resolves_active_entries(world: Any) -> None:
    """生效包的入口都可用，引用冻结版本与摘要；默认由会话承载。"""
    await world.publish()
    alice = await _alice(world)
    found = await world.platform.artisans.available(alice)
    assert found and {a.ref.package for a in found} == {"support-desk"}
    one = found[0]
    assert one.ref.version == "1.0.0" and len(one.ref.digest) == 64
    assert one.ref.decision_ref == "catalog:support-desk@1.0.0"
    assert one.host == "session" and one.entry_description
    assert await world.platform.artisans.resolve(alice, "support-desk", one.ref.entry) == one


async def test_unavailable_is_not_found(world: Any) -> None:
    """未生效（没有任何版本）、不存在的包、未发布的入口一律 404。"""
    alice = await _alice(world)
    assert await world.platform.artisans.available(alice) == ()
    with pytest.raises(NotFoundError):
        await world.platform.artisans.resolve(alice, "support-desk", "ticket")
    await world.publish()
    for package, entry in (("nope", "ticket"), ("support-desk", "not-an-entry")):
        with pytest.raises(NotFoundError):
            await world.platform.artisans.resolve(alice, package, entry)


async def test_inactive_version_is_not_used(world: Any) -> None:
    """只上传、未设为生效的新版本不会被解析成匠人。"""
    await world.publish()
    await world.platform.packages.publish(world.admin, world.archive(version="2.0.0"),
                                          activate=False)
    alice = await _alice(world)
    assert {a.ref.version for a in await world.platform.artisans.available(alice)} == {"1.0.0"}


async def test_disabled_employee_and_machine_identity_refused(world: Any) -> None:
    """停用员工与不代表员工的 API key 都不能解析匠人。"""
    await world.publish()
    alice = await _alice(world)
    await world.platform.identity.store.set_user_status(
        "acme", "usr_alice", "disabled", reason="admin", at=time.time())
    for who in (alice, world.member):
        with pytest.raises(PermissionDeniedError):
            await world.platform.artisans.available(who)
        with pytest.raises(PermissionDeniedError):
            await world.platform.artisans.resolve(who, "support-desk", "ticket")


async def test_durable_tenant_and_offline_package_use_tasks(world: Any) -> None:
    """持久执行开启且包断网时由任务承载，与 from-capability 同口径。"""
    from xuanpu_server.butler.resolver import CatalogArtisanResolver

    await world.publish()
    alice = await _alice(world)
    platform = world.platform
    durable = DurableExecutionConfig(enabled=True, tenant_ids=("acme",), verified_text_bounds=True)
    resolver = CatalogArtisanResolver(platform.packages, platform.workspace, durable)
    hosts = {a.host for a in await resolver.available(alice)}
    networked = (await platform.packages.resolve("acme", "support-desk")).manifest.spec.policy \
        .sandbox.network
    assert hosts == ({"session"} if networked else {"task"})
