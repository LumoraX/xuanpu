"""开关、幂等与降级：未开启走旧分流；同键同体回原回执、不同体 409；失败保留输入可重试。"""

from __future__ import annotations

from typing import Any

import httpx
from xuanpu_server.api.app import create_app
from xuanpu_server.api.auth import caller
from xuanpu_server.tracing.models import TraceOwner

from .conftest import employee, enable

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")


async def test_disabled_tenant_uses_existing_route(world: Any, step: Any) -> None:
    """管家未开启：同一入口走既有分流，回执标 route。"""
    enable(world, butler=False)
    await world.publish()
    alice = await employee(world)
    world.models.script(step(text='{"package":null,"entry":null,"message":"好的，我来帮你。"}'))
    app = create_app(world.platform)
    app.dependency_overrides[caller] = lambda: alice
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        response = await http.post("/v1/assistant/messages",
                                   json={"request_id": "r1", "text": "你好"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "route" and body["message"] == "好的，我来帮你。"
    assert "dispatches" not in body


async def test_same_request_replays_and_different_body_conflicts(
    butler: dict[str, Any], step: Any,
) -> None:
    """同键同体：原回执，不再采样；同键异体：409。"""
    world, http = butler["world"], butler["http"]
    world.models.script(step(text="收到。"))
    first = (await http.post("/v1/assistant/messages",
                             json={"request_id": "k1", "text": "你好"})).json()
    sampled = len(world.models.requests)
    again = (await http.post("/v1/assistant/messages",
                             json={"request_id": "k1", "text": "你好"})).json()
    assert again == first and len(world.models.requests) == sampled
    conflict = await http.post("/v1/assistant/messages",
                               json={"request_id": "k1", "text": "换个说法"})
    assert conflict.status_code == 409


async def test_failure_keeps_input_and_retry_succeeds(butler: dict[str, Any], step: Any) -> None:
    """管家失败：503、原话已在主对话、不选任何业务；同一标识重试成功。"""
    world, http = butler["world"], butler["http"]
    engines = world.platform.butler.engines
    original = engines.run

    async def broken(*_args: Any, **_kwargs: Any) -> str:
        """模拟模型不可用。"""
        raise RuntimeError("model down")

    engines.run = broken  # type: ignore[method-assign]
    failed = await http.post("/v1/assistant/messages",
                             json={"request_id": "f1", "text": "帮我看看工单"})
    assert failed.status_code == 503
    texts = [m["text"] for m in await world.platform.workspace.messages(butler["alice"], limit=10)]
    assert texts == ["帮我看看工单"]
    assert world.platform.store.dispatch_store is not None
    engines.run = original  # type: ignore[method-assign]
    world.models.script(step(text="我看一下。"))
    retried = await http.post("/v1/assistant/messages",
                              json={"request_id": "f1", "text": "帮我看看工单"})
    assert retried.status_code == 200 and retried.json()["message"] == "我看一下。"


async def test_machine_identity_falls_back_to_route(butler: dict[str, Any], step: Any) -> None:
    """不代表员工的 API key 不进管家（管家每员工一个），走既有分流。"""
    world = butler["world"]
    butler["as"]["who"] = world.member
    world.models.script(step(text='{"package":null,"entry":null,"message":"你好"}'))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": "m1", "text": "你好"})
    assert response.status_code == 200 and response.json()["mode"] == "route"
