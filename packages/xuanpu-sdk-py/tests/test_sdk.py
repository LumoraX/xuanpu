"""SDK 的解析与错误映射：用桩传输，不需要平台。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from xuanpu_sdk import Event, XuanpuClient, XuanpuError, XuanpuTransportError
from xuanpu_sdk.sse import parse_sse


async def _lines(text: str) -> Any:
    """把文本按行产出。"""
    for line in text.splitlines(keepends=True):
        yield line


async def test_parse_sse_skips_comments_and_joins_data() -> None:
    """注释行跳过；多行 data 拼接；空 data 得到空对象。"""
    text = (
        ": keepalive\n\n"
        'event: a\ndata: {"x":\n'
        "data: 1}\n\n"
        "event: b\ndata:\n\n"
        "event: c\ndata: {}\n\n"
    )
    frames = [item async for item in parse_sse(_lines(text))]
    assert frames == [("a", {"x": 1}), ("b", {}), ("c", {})]


async def test_parse_sse_rejects_non_json() -> None:
    """数据不是 JSON 时报传输错误。"""
    with pytest.raises(XuanpuTransportError):
        _ = [item async for item in parse_sse(_lines("event: a\ndata: nope\n\n"))]


def _stub(handler: Any) -> httpx.MockTransport:
    """桩传输。"""
    return httpx.MockTransport(handler)


async def test_error_mapping() -> None:
    """平台的错误响应映射成异常，附带额外字段。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(
            409,
            json={"error": {"code": "session_busy", "message": "被占", "owner": "inst-2"}},
        )

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        with pytest.raises(XuanpuError) as caught:
            await client.sessions.get("s1")
    assert (caught.value.status, caught.value.code, caught.value.message) == (
        409, "session_busy", "被占",
    )  # fmt: skip
    assert caught.value.details == {"owner": "inst-2"}


async def test_non_platform_error_is_transport_error() -> None:
    """不是平台格式的错误响应（比如反向代理的 502 页面）按传输错误处理。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        return httpx.Response(502, text="<html>bad gateway</html>")

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        with pytest.raises(XuanpuTransportError, match="502"):
            await client.health()


async def test_stream_yields_events_until_terminal() -> None:
    """事件流：turn / end 帧不产出；ask 把文本拼好并返回终结事件。"""
    frames = [
        ("turn", {"session_id": "s1", "submission_id": "sub"}),
        (
            "turn_started",
            {"kind": "turn_started", "submission_id": "sub", "seq": 0, "at": 1.0, "data": {}},
        ),
        (
            "assistant_text",
            {
                "kind": "assistant_text",
                "submission_id": "sub",
                "seq": 1,
                "at": 1.0,
                "data": {"delta": "你"},
            },
        ),
        (
            "assistant_text",
            {
                "kind": "assistant_text",
                "submission_id": "sub",
                "seq": 2,
                "at": 1.0,
                "data": {"delta": "好"},
            },
        ),
        (
            "turn_completed",
            {
                "kind": "turn_completed",
                "submission_id": "sub",
                "seq": 3,
                "at": 1.0,
                "data": {"is_root": True},
            },
        ),
        ("end", {"submission_id": "sub", "overflowed": False}),
    ]
    body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in frames)

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        assert json.loads(request.content) == {"text": "hi"}
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        events = [event async for event in client.sessions.send("s1", "hi")]
        reply, last = await client.sessions.ask("s1", "hi")
    assert [event.kind for event in events] == [
        "turn_started",
        "assistant_text",
        "assistant_text",
        "turn_completed",
    ]
    assert all(isinstance(event, Event) for event in events)
    assert (reply, last.kind, last.terminal) == ("你好", "turn_completed", True)


def test_rejected_or_partial_resume_is_terminal() -> None:
    """送回审批被拒或只核销了一部分：没有续跑，这条事件就是终结事件（与平台口径一致）。"""
    for kind in ("suspension_resolve_rejected", "suspension_partially_resolved"):
        assert Event(kind=kind, submission_id="sub", seq=1, at=1.0).terminal
    assert not Event(kind="suspension_resolved", submission_id="sub", seq=1, at=1.0).terminal


async def test_stream_error_response_raises() -> None:
    """事件流请求被拒时抛平台错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "没有"}})

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        with pytest.raises(XuanpuError) as caught:
            _ = [event async for event in client.sessions.send("s1", "hi")]
    assert caught.value.code == "not_found"


async def test_truncated_stream_is_reported() -> None:
    """事件流在本轮结束前断开：ask 明确报错，不把半截回复当成完整回复。"""
    body = 'event: turn\ndata: {"session_id": "s", "submission_id": "sub"}\n\nevent: assistant_text\ndata: {"kind": "assistant_text", "submission_id": "sub", "seq": 1, "at": 1.0, "data": {"delta": "半"}}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        return httpx.Response(200, text=body)

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        with pytest.raises(XuanpuTransportError, match="断开"):
            await client.sessions.ask("s1", "hi")


async def test_cancel_posts_to_the_session() -> None:
    """停止生成：不指定时请求体为空对象，指定时带上 submission；返回停了哪些。"""
    seen: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        body = json.loads(request.content)
        seen.append((request.method, request.url.path, body))
        cancelled = [body["submission_id"]] if "submission_id" in body else ["sub_a", "sub_b"]
        return httpx.Response(200, json={"cancelled": cancelled})

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        assert await client.sessions.cancel("s1") == ["sub_a", "sub_b"]
        assert await client.sessions.cancel("s1", submission_id="sub_a") == ["sub_a"]
    assert seen == [
        ("POST", "/v1/sessions/s1/cancel", {}),
        ("POST", "/v1/sessions/s1/cancel", {"submission_id": "sub_a"}),
    ]


async def test_audit_pagination() -> None:
    """审计查询自动翻页。"""
    pages = {
        0: {"records": [{"audit_id": 1}, {"audit_id": 2}], "next_after_id": 2},
        2: {"records": [{"audit_id": 3}], "next_after_id": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """返回本用例预设的公开HTTP响应。"""
        after = int(request.url.params["after_id"])
        page = pages[after]
        records = [
            {
                "audit_id": item["audit_id"], "source": "control", "kind": "k", "at": 1.0,
                "actor": "a", "session_id": None, "stream": None, "seq": None, "data": {},
            }
            for item in page["records"]
        ]  # fmt: skip
        return httpx.Response(
            200, json={"records": records, "next_after_id": page["next_after_id"]}
        )

    async with XuanpuClient("http://x", "k", transport=_stub(handler)) as client:
        ids = [record.audit_id async for record in client.admin.audit(limit=2)]
    assert ids == [1, 2, 3]


def test_operation_terminal_projection_overrides_leaf_and_allows_abort() -> None:
    """新服务端的操作级布尔优先，旧服务器和未知事件仍保持明确边界。"""
    from xuanpu_sdk import Event

    assert not Event(kind='turn_suspended', submission_id='s', seq=1, at=1,
                     data={'operation_terminal': False}).terminal
    assert Event(kind='turn_suspended', submission_id='s', seq=2, at=1,
                 data={'operation_terminal': True}).terminal
    assert Event(kind='suspension_resolved', submission_id='s', seq=3, at=1,
                 data={'aborted': True, 'operation_terminal': True}).terminal
    assert not Event(kind='future_kind', submission_id='s', seq=4, at=1,
                     data={'operation_terminal': True}).terminal
    assert Event(kind='turn_suspended', submission_id='s', seq=5, at=1).terminal


@pytest.mark.parametrize('seq', [None, 7])
async def test_replay_event_preserves_nullable_or_integer_sequence(seq: int | None) -> None:
    """回放合成全文没有流序号，SDK原样保留None而不伪造整数。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """返回公开回放可出现的真实帧形状。"""
        payload = {'kind': 'turn_text', 'submission_id': 'sub', 'seq': seq,
                   'at': 1, 'data': {'text': '完整回复', 'operation_terminal': True}}
        return httpx.Response(200, text='event: turn_text\ndata: ' + json.dumps(payload) + '\n\n')

    async with XuanpuClient('http://x', 'k', transport=_stub(handler)) as client:
        events = [item async for item in client.sessions.resume('s', {'r': {'granted': True}})]
    assert events[0].seq == seq and not events[0].terminal


@pytest.mark.parametrize('seq', ['missing', False, 1.2, '3', {}])
async def test_replay_event_rejects_missing_or_invalid_sequence(seq: Any) -> None:
    """缺字段或错误类型不能当作可空审计序号悄悄接受。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """让公开SDK解析器面对不合法的服务端事件帧。"""
        payload = {'kind': 'turn_text', 'submission_id': 'sub', 'at': 1, 'data': {}}
        if seq != 'missing':
            payload['seq'] = seq
        return httpx.Response(200, text='event: turn_text\ndata: ' + json.dumps(payload) + '\n\n')

    async with XuanpuClient('http://x', 'k', transport=_stub(handler)) as client:
        with pytest.raises(XuanpuTransportError, match='seq'):
            _ = [item async for item in client.sessions.resume('s', {'r': {'granted': True}})]
