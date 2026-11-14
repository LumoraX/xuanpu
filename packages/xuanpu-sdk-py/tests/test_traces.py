"""执行追踪只读接口：路径编码、分页参数与代员工身份原样传递。"""
from __future__ import annotations

import httpx
from xuanpu_sdk import XuanpuClient


async def test_trace_reads_preserve_identity_and_encode_ids():
    """标识只占一个路径段；分页参数原样；每个请求都带代员工头。"""
    calls = []

    def receive(request):
        calls.append(request)
        if request.url.path == "/v1/traces":
            return httpx.Response(200, json={"items": [], "next_cursor": None, "total": 0})
        return httpx.Response(200, json={"trace_id": "t", "spans": [], "rows": []})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY", act_as_user="member",
                            transport=httpx.MockTransport(receive)) as client:
        assert (await client.list_traces(active=True, cursor="c1", limit=10))["total"] == 0
        await client.get_trace("a/b")
        await client.trace_waterfall("a/b")
    assert dict(calls[0].url.params) == {"limit": "10", "active": "true", "cursor": "c1"}
    assert calls[1].url.raw_path == b"/v1/traces/a%2Fb"
    assert calls[2].url.raw_path == b"/v1/traces/a%2Fb/waterfall"
    assert all(r.method == "GET" and r.headers["X-Xuanpu-Act-As"] == "member" for r in calls)


async def test_follow_trace_parses_frames_and_resumes():
    """SSE 帧按事件名产出；重连序号经 Last-Event-ID 传给服务端；心跳跳过。"""
    seen = []
    body = (
        "id: 2\nevent: snapshot\ndata: {\"spans\": [], \"active\": true}\n\n"
        ": heartbeat\n\n"
        "id: 3\nevent: end\ndata: {\"active\": false}\n\n"
    )

    def receive(request):
        seen.append(request)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY", act_as_user="member",
                            transport=httpx.MockTransport(receive)) as client:
        frames = [name async for name, _ in client.follow_trace("a/b", after_seq=1)]
    assert frames == ["snapshot", "end"]
    assert seen[0].url.raw_path == b"/v1/traces/a%2Fb/events"
    assert seen[0].headers["Last-Event-ID"] == "1"
    assert seen[0].headers["X-Xuanpu-Act-As"] == "member"


async def test_send_to_butler_posts_request_id_and_body():
    """管家入口：幂等标识与原话原样提交，带代员工头。"""
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={"mode": "butler", "trace_id": "t", "dispatches": []})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY", act_as_user="member",
                            transport=httpx.MockTransport(receive)) as client:
        receipt = await client.send_to_butler("帮我查工单", request_id="r1",
                                              input_files=[{"item_id": "f", "revision": 2}])
    assert receipt["mode"] == "butler"
    import json
    assert seen[0].method == "POST" and seen[0].url.path == "/v1/assistant/messages"
    assert json.loads(seen[0].content) == {
        "request_id": "r1", "text": "帮我查工单", "timezone": "Asia/Shanghai",
        "input_files": [{"item_id": "f", "revision": 2}]}
    assert seen[0].headers["X-Xuanpu-Act-As"] == "member"


async def test_butler_pending_list_and_answer():
    """待处理项：列表与答复；标识只占一个路径段，原话与幂等标识原样提交。"""
    import json

    seen = []

    def receive(request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": [{"pending_id": "p/1"}]})
        return httpx.Response(200, json={"pending_id": "p/1", "state": "answered"})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY", act_as_user="member",
                            transport=httpx.MockTransport(receive)) as client:
        assert await client.list_butler_pending() == [{"pending_id": "p/1"}]
        result = await client.answer_butler_pending("p/1", "138", request_id="a1")
    assert result["state"] == "answered"
    assert seen[0].url.path == "/v1/assistant/pending"
    assert seen[1].url.raw_path == b"/v1/assistant/pending/p%2F1/answer"
    assert json.loads(seen[1].content) == {"request_id": "a1", "text": "138"}


async def test_butler_group_reads_encode_ids():
    """编组与消息区只读；标识只占一个路径段；每个请求带代员工头。"""
    calls = []

    def receive(request):
        calls.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"items": [{"seq": 1, "text": "结论"}]})
        return httpx.Response(200, json={"group_id": "g/1", "state": "running"})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY", act_as_user="member",
                            transport=httpx.MockTransport(receive)) as client:
        assert (await client.get_butler_group("g/1"))["state"] == "running"
        assert await client.list_group_messages("g/1") == [{"seq": 1, "text": "结论"}]
    assert calls[0].url.raw_path == b"/v1/assistant/groups/g%2F1"
    assert calls[1].url.raw_path == b"/v1/assistant/groups/g%2F1/messages"
    assert all(r.method == "GET" and r.headers["X-Xuanpu-Act-As"] == "member" for r in calls)
