"""两种存储的续期与失败释放契约。"""

from __future__ import annotations

from typing import Any

from xuanpu_server.tenancy import Tenant


async def test_reconcile_renew_release(identity_stores: Any) -> None:
    """过期不能续期，旧持有者不能释放新租约，失败不更新完成时间。"""
    control, store = identity_stores
    await control.create_tenant(Tenant(tenant_id="lease", name="租约", created_at=1))
    assert await store.claim_reconcile("lease", owner="a", now=10, interval=0, lease_seconds=20)
    assert await store.renew_reconcile("lease", owner="a", now=20, lease_seconds=20)
    assert not await store.claim_reconcile("lease", owner="b", now=31, interval=0, lease_seconds=20)
    assert not await store.renew_reconcile("lease", owner="b", now=31, lease_seconds=20)
    assert not await store.renew_reconcile("lease", owner="a", now=40, lease_seconds=20)
    assert await store.claim_reconcile("lease", owner="b", now=40, interval=0, lease_seconds=20)
    assert not await store.release_reconcile("lease", owner="a")
    assert await store.release_reconcile("lease", owner="b")
    state = await store.get_reconcile_state("lease")
    assert state.last_run_at is None and state.lease_owner is None
    assert await store.claim_reconcile("lease", owner="c", now=41, interval=86400, lease_seconds=20)
