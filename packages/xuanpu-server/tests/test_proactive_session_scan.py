"""主动语义评估使用本人业务会话的稳定增量分页。"""

from __future__ import annotations

from dataclasses import replace

import pytest
from xuanpu_server.controlplane.models import SessionRecord
from xuanpu_server.tenancy import Tenant


async def test_scan_user_sessions_pages_all_six_and_isolates_owners(store):
    """六个本人会话分两页读取，创建者和活动时间不改变游标顺序。"""
    for tenant in ("acme", "other"):
        await store.create_tenant(Tenant(tenant_id=tenant, name=tenant, created_at=1.0))
    first = SessionRecord(
        session_id="session-01",
        tenant_id="acme",
        package="support",
        version="1.0.0",
        entry="main",
        created_at=1000.0,
        updated_at=1000.0,
        created_by="user:usr_a",
        user_id="usr_a",
    )
    for index in (6, 2, 4, 1, 5, 3):
        await store.create_session(
            replace(
                first,
                session_id=f"session-{index:02}",
                updated_at=1000.0 + index,
                created_by="different-key" if index == 3 else first.created_by,
            )
        )
    for session in (
        replace(first, session_id="session-00", user_id="usr_b"),
        replace(first, session_id="session-07", user_id=None),
        replace(first, session_id="session-08", tenant_id="other"),
    ):
        await store.create_session(session)
    page_one = await store.scan_user_sessions("acme", "usr_a")
    assert [session.session_id for session in page_one] == [
        f"session-{index:02}" for index in range(1, 6)
    ]
    page_two = await store.scan_user_sessions("acme", "usr_a", page_one[-1].session_id)
    assert [session.session_id for session in page_two] == ["session-06"]
    assert await store.scan_user_sessions("acme", "usr_a", page_two[-1].session_id) == []
    assert [session.session_id for session in await store.scan_user_sessions("other", "usr_a")] == [
        "session-08"
    ]
    assert [session.session_id for session in await store.scan_user_sessions("acme", "usr_b")] == [
        "session-00"
    ]
    assert await store.scan_user_sessions("acme", "missing") == []
    # 原有列表继续按活动时间倒序，扫描接口不改变员工模块原有展示。
    assert [
        session.session_id for session in await store.list_sessions("acme", user_id="usr_a")
    ] == [f"session-{index:02}" for index in range(6, 0, -1)]
    assert [
        session.session_id for session in await store.scan_user_sessions("acme", "usr_a", None, 2)
    ] == ["session-01", "session-02"]


@pytest.mark.parametrize("limit", [0, 101, True])
async def test_scan_user_sessions_rejects_invalid_page_size(store, limit):
    """后台分页必须有界，不能把布尔值当作一条记录。"""
    with pytest.raises(ValueError):
        await store.scan_user_sessions("acme", "usr_a", limit=limit)
