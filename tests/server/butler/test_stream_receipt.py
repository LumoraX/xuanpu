"""流式回执（R7）：先给 trace 标识，再推管家回复的增量，最后推回执；本人可用匠人列表。"""

from __future__ import annotations

import json
from typing import Any

from .conftest import employee

SSE = {"Accept": "text/event-stream"}


def _frames(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """拆出 SSE 帧。"""
    found = []
    for block in raw.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        found.append((lines["event"], json.loads(lines["data"])))
    return found


async def test_stream_starts_with_trace_then_deltas_then_receipt(
    butler: dict[str, Any], step: Any,
) -> None:
    """第一帧就带 trace 标识；回复文本以增量推送，拼起来就是回执里的回复。"""
    butler["world"].models.script(step(text="你好，我是管家。"))
    response = await butler["http"].post(
        "/v1/assistant/messages", headers=SSE, json={"request_id": "s1", "text": "你好"})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _frames(response.text)
    names = [name for name, _ in frames]
    assert names[0] == "started" and names[-1] == "receipt"
    started, receipt = frames[0][1], frames[-1][1]
    assert started["trace_id"] and started["trace_id"] == receipt["trace_id"]
    said = "".join(data["text"] for name, data in frames if name == "delta")
    assert said.strip() == receipt["message"] == "你好，我是管家。"


async def test_stream_json_mode_unchanged(butler: dict[str, Any], step: Any) -> None:
    """不要流时照旧是 JSON 回执。"""
    butler["world"].models.script(step(text="好的。"))
    response = await butler["http"].post("/v1/assistant/messages",
                                         json={"request_id": "s2", "text": "你好"})
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["message"] == "好的。"


async def test_stream_errors_before_stream_keep_status(butler: dict[str, Any]) -> None:
    """不是在职员工：建流前就 403，不会先回 200 再在流里报错。"""
    from server.core.tenancy import Principal

    butler["as"]["who"] = Principal(role="tenant_member", tenant_id="acme", key_id=None,
                                    user_id="usr_ghost")
    response = await butler["http"].post(
        "/v1/assistant/messages", headers=SSE, json={"request_id": "s3", "text": "你好"})
    assert response.status_code == 403


async def test_artisans_list_only_for_employee(butler: dict[str, Any]) -> None:
    """本人可用的匠人；机器身份 403。"""
    items = (await butler["http"].get("/v1/assistant/artisans")).json()["items"]
    assert {(i["package"], i["entry"]) for i in items} >= {("support-desk", "triage")}
    assert all(set(i) == {"package", "entry", "description", "host"} for i in items)
    butler["as"]["who"] = await employee(butler["world"], "usr_bob")
    assert (await butler["http"].get("/v1/assistant/artisans")).status_code == 200
    butler["as"]["who"] = butler["world"].member
    assert (await butler["http"].get("/v1/assistant/artisans")).status_code == 403
