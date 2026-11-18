"""SDK 能力分配（R4-B）：路径、方法与请求体原样提交，标识只占一个路径段。"""

import json

import httpx
from xuanpu_sdk import XuanpuClient


async def test_assign_list_and_revoke_paths():
    """分配、列出、撤销各走自己的接口；标识里的斜杠被转义，不能拼出别的资源路径。"""
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    body = {"request_id": "a1", "selector": {"kind": "user", "id": "usr_a", "user_epoch": 1},
            "allowed_revisions": [1]}
    async with XuanpuClient("https://example.test", "PRIVATE_KEY",
                            transport=httpx.MockTransport(receive)) as client:
        await client.assign_capability("cap_1", body)
        await client.list_capability_assignments("cap_1")
        await client.revoke_capability_assignment("cas/1", {"request_id": "r1", "expected_version": 1})
    assert [(r.method, r.url.raw_path.decode()) for r in seen] == [
        ("POST", "/v1/capabilities/cap_1/assignments"),
        ("GET", "/v1/capabilities/cap_1/assignments"),
        ("POST", "/v1/capability-assignments/cas%2F1/revoke"),
    ]
    assert json.loads(seen[0].content) == body
