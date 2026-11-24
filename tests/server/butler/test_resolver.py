"""匠人解析：本人可用的生效入口；未生效、未发布、停用员工、机器身份一律不可用。"""

from __future__ import annotations

import time
from typing import Any

import pytest

from server.core.errors import NotFoundError, PermissionDeniedError
from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.domains.work_tasks.config import DurableExecutionConfig


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
    assert one.ref.display_name == "工单助手", "派工冻结匠人显示名，员工看到的是它而不是包名"
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
    from server.engine.butler.resolver import CatalogArtisanResolver

    await world.publish()
    alice = await _alice(world)
    platform = world.platform
    durable = DurableExecutionConfig(enabled=True, tenant_ids=("acme",), verified_text_bounds=True)
    resolver = CatalogArtisanResolver(platform.packages, platform.workspace, durable)
    hosts = {a.host for a in await resolver.available(alice)}
    networked = (await platform.packages.resolve("acme", "support-desk")).manifest.spec.policy \
        .sandbox.network
    assert hosts == ({"session"} if networked else {"task"})


def test_wake_messages_use_display_name_and_fall_back() -> None:
    """回报里的匠人名用显示名；旧派工记录没有显示名时用包名。"""
    from server.engine.butler.models import ArtisanRef
    from server.engine.butler.wake_apply import _name

    base = {"package": "support-desk", "version": "1.0.0", "digest": "a" * 64, "entry": "triage",
            "decision_ref": "catalog:support-desk@1.0.0"}

    class Dispatch:
        """只带匠人引用的派工替身。"""

        def __init__(self, artisan: ArtisanRef) -> None:
            self.artisan = artisan

    assert _name(Dispatch(ArtisanRef(**base, display_name="医生匠人"))) == "医生匠人"
    assert _name(Dispatch(ArtisanRef(**base))) == "support-desk"
