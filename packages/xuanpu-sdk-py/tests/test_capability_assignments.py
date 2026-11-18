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


async def test_evaluation_and_release_paths():
    """评测与发布各走自己的接口；停止/撤销之外的动作在本地就拒绝。"""
    import pytest

    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY",
                            transport=httpx.MockTransport(receive)) as client:
        await client.evaluate_capability({"request_id": "e"})
        await client.get_capability_evaluation("cev_1")
        await client.preview_capability_release("cap_1", {})
        await client.release_capability("cap_1", {})
        await client.list_capability_releases("cap_1")
        await client.change_capability_release("crl_1", "revoke", {"request_id": "r", "expected_version": 1})
        with pytest.raises(ValueError):
            await client.change_capability_release("crl_1", "delete", {})
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/capability-evaluations"), ("GET", "/v1/capability-evaluations/cev_1"),
        ("POST", "/v1/capabilities/cap_1/release-previews"), ("POST", "/v1/capabilities/cap_1/releases"),
        ("GET", "/v1/capabilities/cap_1/releases"), ("POST", "/v1/capability-releases/crl_1/revoke")]


async def test_drain_preview_path():
    """排空预览只读，包名作为查询参数。"""
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    async with XuanpuClient("https://example.test", "PRIVATE_KEY",
                            transport=httpx.MockTransport(receive)) as client:
        await client.capability_drain_preview("office-review")
    assert [(r.method, r.url.path, dict(r.url.params)) for r in seen] == [
        ("GET", "/v1/admin/capability-drain", {"package": "office-review"})]
