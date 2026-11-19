"""SDK 本人知识、问答、工作记忆与能力结果（R4-D/F）：路径、方法与请求体原样提交，标识只占一个路径段。"""

import json

import httpx
import pytest
from xuanpu_sdk import XuanpuClient


def _recorder():
    """记录请求的假传输层。"""
    seen = []

    def receive(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return seen, httpx.MockTransport(receive)


async def test_knowledge_and_context_paths():
    """来源、检索、问答、记忆各走自己的接口；非法动作本地拒绝。"""
    seen, transport = _recorder()
    async with XuanpuClient("https://example.test", "PRIVATE_KEY", transport=transport) as client:
        await client.register_knowledge_source("itm_1", 2)
        await client.register_shared_knowledge_source("shm_1", "mat_1", 1)
        await client.list_knowledge_sources()
        await client.search_knowledge("预算", limit=3)
        await client.change_knowledge_source("ksr/1", "refresh", 1)
        await client.create_knowledge_context("c1", [{"source_id": "ksr_1", "ordinal": 0}])
        await client.list_knowledge_contexts()
        await client.read_knowledge_context("kcx_1")
        await client.ask_knowledge("kcx_1", "q1", "口径？")
        await client.close_knowledge_context("kcx_1", 3)
        await client.list_work_memory()
        await client.propose_work_memory({"text": "x", "citation": {"source_id": "ksr_1", "ordinal": 0}})
        await client.assert_work_memory({"text": "y"})
        await client.decide_work_memory("wm_1", "confirm", 1)
        await client.correct_work_memory("wm_1", 2, "z")
        with pytest.raises(ValueError):
            await client.change_knowledge_source("ksr_1", "purge", 1)
        with pytest.raises(ValueError):
            await client.decide_work_memory("wm_1", "share", 1)
    assert [(r.method, r.url.raw_path.decode()) for r in seen] == [
        ("POST", "/v1/me/knowledge/sources"), ("POST", "/v1/me/knowledge/sources"), ("GET", "/v1/me/knowledge/sources"),
        ("POST", "/v1/me/knowledge/search"), ("POST", "/v1/me/knowledge/sources/ksr%2F1/refresh"),
        ("POST", "/v1/me/knowledge/contexts"), ("GET", "/v1/me/knowledge/contexts"),
        ("GET", "/v1/me/knowledge/contexts/kcx_1"), ("POST", "/v1/me/knowledge/contexts/kcx_1/ask"),
        ("POST", "/v1/me/knowledge/contexts/kcx_1/close"), ("GET", "/v1/me/work-memory"),
        ("POST", "/v1/me/work-memory/proposals"), ("POST", "/v1/me/work-memory/assertions"),
        ("POST", "/v1/me/work-memory/wm_1/confirm"), ("POST", "/v1/me/work-memory/wm_1/correct")]
    assert json.loads(seen[0].content) == {"item_id": "itm_1", "revision": 2}
    assert json.loads(seen[1].content)["kind"] == "shared_material"
    assert json.loads(seen[8].content) == {"request_id": "q1", "question": "口径？"}


async def test_outcome_and_acceptance_paths():
    """统计参数只带给定的；批次与观察各走自己的接口。"""
    seen, transport = _recorder()
    async with XuanpuClient("https://example.test", "PRIVATE_KEY", transport=transport) as client:
        await client.my_capability_outcomes(since=10.0, limit=5)
        await client.admin_capability_outcomes()
        await client.create_acceptance_batch({"request_id": "b"})
        await client.get_acceptance_batch("acb_1")
        await client.observe_acceptance("acb_1", {"request_id": "o"})
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/v1/me/capability-outcomes"), ("GET", "/v1/admin/capability-outcomes"),
        ("POST", "/v1/admin/acceptance-batches"), ("GET", "/v1/acceptance-batches/acb_1"),
        ("POST", "/v1/acceptance-batches/acb_1/observations")]
    assert dict(seen[0].url.params) == {"limit": "5", "offset": "0", "since": "10.0"}
    assert dict(seen[1].url.params) == {}
