"""执行追踪只读接口：本人可见、他人 404、机器身份拒绝、分页与瀑布形状。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest

from server.api.app import create_app
from server.api.auth import caller

from .test_platform_tracing import _employee, _enable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from server.core.tenancy import Principal


@pytest.fixture
async def traced(world: Any, step: Any, read_turn: Any) -> AsyncIterator[dict[str, Any]]:
    """Alice 跑过两轮对话；Bob 是同租户另一名员工。"""
    _enable(world, "acme")
    await world.publish()
    alice = await _employee(world, "usr_alice")
    bob = await _employee(world, "usr_bob")
    session = await world.platform.sessions.create(alice, package="support-desk")
    for text in ("一", "二"):
        world.models.script(step(text=f"收到{text}"))
        await read_turn(await world.platform.sessions.send_message(alice, session.session_id, text))
    await world.platform.sessions.close(alice, session.session_id)
    app = create_app(world.platform)
    current: dict[str, Principal] = {}
    app.dependency_overrides[caller] = lambda: current["who"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        yield {"http": http, "as": current, "alice": alice, "bob": bob, "key": world.member,
               "session": session.session_id}


async def test_owner_lists_reads_tree_and_waterfall(traced: dict[str, Any]) -> None:
    """本人：列表倒序分页、执行树、瀑布行。"""
    http, traced["as"]["who"] = traced["http"], traced["alice"]
    first = (await http.get("/v1/traces", params={"limit": 1})).json()
    assert first["total"] == 2 and len(first["items"]) == 1 and first["next_cursor"]
    second = (await http.get("/v1/traces", params={"limit": 1, "cursor": first["next_cursor"]})).json()
    assert second["next_cursor"] is None
    assert first["items"][0]["created_at"] >= second["items"][0]["created_at"]
    trace_id = first["items"][0]["trace_id"]
    tree = (await http.get(f"/v1/traces/{trace_id}")).json()
    assert tree["active"] is False and tree["incomplete"] is False
    root = [s for s in tree["spans"] if s["parent_span_id"] is None]
    assert len(root) == 1 and root[0]["ref"] == traced["session"] and root[0]["state"] == "completed"
    assert set(root[0]) >= {"input_tokens", "output_tokens", "usage_complete", "attempts"}
    assert not {"arguments", "output", "text", "content"} & set(root[0])
    rows = (await http.get(f"/v1/traces/{trace_id}/waterfall")).json()["rows"]
    assert rows[0]["depth"] == 0 and rows[0]["duration_ms"] is not None


async def test_other_employee_sees_nothing(traced: dict[str, Any]) -> None:
    """同租户另一名员工：列表为空，读取 404，不暴露存在性。"""
    http = traced["http"]
    traced["as"]["who"] = traced["alice"]
    trace_id = (await http.get("/v1/traces")).json()["items"][0]["trace_id"]
    traced["as"]["who"] = traced["bob"]
    assert (await http.get("/v1/traces")).json()["total"] == 0
    for path in (f"/v1/traces/{trace_id}", f"/v1/traces/{trace_id}/waterfall"):
        assert (await http.get(path)).status_code == 404


async def test_machine_identity_is_refused(traced: dict[str, Any]) -> None:
    """不代表员工的 API key 不能读执行记录。"""
    traced["as"]["who"] = traced["key"]
    assert (await traced["http"].get("/v1/traces")).status_code == 403


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"cursor": "!!bad!!"}])
async def test_bad_paging_is_rejected(traced: dict[str, Any], params: dict[str, Any]) -> None:
    """分页参数越界或游标损坏是请求错误。"""
    traced["as"]["who"] = traced["alice"]
    assert (await traced["http"].get("/v1/traces", params=params)).status_code in (400, 422)


async def test_events_stream_for_finished_trace_and_isolation(traced: dict[str, Any]) -> None:
    """已结束的 trace：快照后立即 end；他人 404；游标损坏 400。"""
    http = traced["http"]
    traced["as"]["who"] = traced["alice"]
    trace_id = (await http.get("/v1/traces")).json()["items"][0]["trace_id"]
    response = await http.get(f"/v1/traces/{trace_id}/events")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [line.split(": ", 1)[1] for line in response.text.splitlines()
              if line.startswith("event: ")]
    assert events == ["snapshot", "end"]
    bad = await http.get(f"/v1/traces/{trace_id}/events", headers={"Last-Event-ID": "x"})
    assert bad.status_code == 400
    traced["as"]["who"] = traced["bob"]
    assert (await http.get(f"/v1/traces/{trace_id}/events")).status_code == 404
