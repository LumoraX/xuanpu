"""会话的业务关联标识与按创建者筛选。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from xuanpu_sdk import XuanpuClient
from xuanpu_server.api import create_app
from xuanpu_server.controlplane import AuditQuery
from xuanpu_server.errors import InvalidRequestError


async def test_metadata_round_trips_and_is_audited(world: Any) -> None:
    """标识原样存取；写进 session_created 审计。"""
    await world.publish()
    labels = {"customer_id": "C-42", "ticket": "T-1", "备注": "含 中文"}
    session = await world.platform.sessions.create(
        world.member, package="support-desk", metadata=labels
    )
    assert session.metadata == labels
    stored = await world.platform.sessions.get(world.member, session.session_id)
    assert stored.metadata == labels
    audit = await world.platform.store.query_audit("acme", AuditQuery(kind="session_created"))
    assert audit[-1].data["metadata"] == labels


@pytest.mark.parametrize(
    "metadata",
    [
        {f"k{i}": "v" for i in range(17)},
        {"": "v"},
        {"k": "x" * 257},
        {"x" * 257: "v"},
    ],
)
async def test_invalid_metadata_rejected(world: Any, metadata: dict[str, str]) -> None:
    """条数与长度受限。"""
    await world.publish()
    with pytest.raises(InvalidRequestError, match="metadata"):
        await world.platform.sessions.create(
            world.member, package="support-desk", metadata=metadata
        )


async def test_list_mine_filters_by_creator(world: Any) -> None:
    """``mine`` 只看本凭据创建的会话。"""
    await world.publish()
    by_member = await world.platform.sessions.create(world.member, package="support-desk")
    by_admin = await world.platform.sessions.create(world.admin, package="support-desk")
    everyone = await world.platform.sessions.list_sessions(world.member)
    assert {s.session_id for s in everyone} == {by_member.session_id, by_admin.session_id}
    mine = await world.platform.sessions.list_sessions(world.member, mine=True)
    assert [s.session_id for s in mine] == [by_member.session_id]


async def test_metadata_over_http_and_sdk(world: Any) -> None:
    """经 HTTP 与 SDK：创建时带标识，列出时可只看自己的。"""
    await world.publish()
    transport = httpx.ASGITransport(app=create_app(world.platform))
    async with XuanpuClient("http://xuanpu", world.member_key, transport=transport) as client:
        session = await client.sessions.create("support-desk", metadata={"customer_id": "C-1"})
        assert session.metadata == {"customer_id": "C-1"}
        assert (await client.sessions.get(session.session_id)).metadata == {"customer_id": "C-1"}
        assert [s.session_id for s in await client.sessions.list(mine=True)] == [session.session_id]
    async with httpx.AsyncClient(transport=transport, base_url="http://xuanpu") as http:
        bad = await http.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {world.member_key}"},
            json={"package": "support-desk", "metadata": {"n": 1}},
        )
        assert bad.status_code == 400
