"""归档应发送真实材料并逐版本读回，不能接受虚假成功。"""
from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from xuanpu_finance.compliance.archive import ArchivePipeline
from xuanpu_finance.compliance.protocol import ComplianceProviderConfig
from xuanpu_finance.compliance.provider import ComplianceRegistry
from xuanpu_finance.errors import FinanceError
from xuanpu_finance.objects import EncryptedLocalObjects
from xuanpu_finance.operations import request_digest


def archive_request() -> dict[str, Any]:
    """固定一个明确模拟的原件和内部快照包。"""
    raw = b'{"simulation":true,"invoice":"sample"}'
    snapshot = dict(target={"type": "invoice", "id": "i1", "version": 1},
        content_version=1, compliance_version=1, event_cutoff=datetime.now(UTC).isoformat(),
        simulation=True, profile={"id": "simulation-archive-v1", "version": 1},
        retention={"retention_class": "test", "days": 1, "anchor_at": datetime.now(UTC).isoformat(),
                   "required_lock_mode": "SIMULATION", "required_legal_hold": False},
        parent_manifest_hash=None, snapshots={}, required_roles=["original"], artifacts=[
            dict(id="original1", path="original/invoice.json", kind="original", size=len(raw),
                 sha256=hashlib.sha256(raw).hexdigest(), media_type="application/json",
                 source={"content_base64": base64.b64encode(raw).decode()})])
    snapshot["source_snapshot_hash"] = request_digest(snapshot)
    req = dict(protocol_version="1", tenant_id="t1", legal_entity_id="c1", operation_id="f3op_a1",
        action="archive", external_request_id="archive1", provider="test-archive", adapter_version="v1",
        configuration_version=1, simulation=True, amount="10.00", currency="CNY",
        target={"type": "invoice", "id": "i1", "version": 1}, plan_digest="a"*64,
        payload={"archive_snapshot": snapshot})
    req["payload_hash"] = request_digest(req)
    return req


def provider(tmp_path: Any, client: httpx.AsyncClient) -> ComplianceRegistry:
    """注入明确支持版本和保留锁读回的测试渠道。"""
    cfg = ComplianceProviderConfig.model_validate(dict(tenant_id="t1", legal_entity_id="c1",
        provider="test-archive", kind="archive", configuration_version=1, adapter_version="v1",
        base_url="http://127.0.0.1:9999", token_file=tmp_path/"t", callback_secret_file=tmp_path/"s",
        callback_key_id="k", simulation=True, supports_idempotency=True, supports_query=True,
        supports_object_versions=True, supports_retention_readback=True))
    return ComplianceRegistry([cfg], client, tokens={("test-archive",1):"token"},
                              callback_secrets={("test-archive",1):"s"*32})


async def test_preflight_saves_actual_fixed_bundle(tmp_path: Any) -> None:
    """原件字节在发送前进入自己的加密物化包。"""
    objects = EncryptedLocalObjects(tmp_path/"objects", b"x"*32)
    async with httpx.AsyncClient() as client:
        pipeline = ArchivePipeline(provider(tmp_path, client), objects)
        request = archive_request()
        descriptor = await pipeline.preflight(request)
        assert descriptor["manifest_hash"]
        assert len(await objects.get(descriptor["bundle_key"])) > 100
        assert await pipeline.preflight(request) == descriptor


@pytest.mark.parametrize("change", ["hash", "missing", "traversal", "formal"])
async def test_preflight_rejects_missing_or_uncontrolled_material(tmp_path: Any, change: str) -> None:
    """材料摘要、清册、路径和标准必须在实际发送前核对。"""
    req = archive_request()
    snapshot = req["payload"]["archive_snapshot"]
    if change == "hash":
        snapshot["artifacts"][0]["sha256"] = "b"*64
    elif change == "missing":
        snapshot["required_roles"].append("voucher")
    elif change == "traversal":
        snapshot["artifacts"][0]["path"] = "../escape.json"
    else:
        snapshot["profile"]["id"] = "official-unverified"
    snapshot["source_snapshot_hash"] = request_digest({k:v for k,v in snapshot.items() if k != "source_snapshot_hash"})
    req["payload_hash"] = request_digest({k:v for k,v in req.items() if k != "payload_hash"})
    async with httpx.AsyncClient() as client:
        pipeline = ArchivePipeline(provider(tmp_path, client), EncryptedLocalObjects(tmp_path/"o",b"x"*32))
        with pytest.raises(FinanceError):
            await pipeline.preflight(req)


async def test_success_reads_every_member_and_query_never_uploads(tmp_path: Any) -> None:
    """供应商回显成功仍须读所有成员及锁，query再执行同样核验。"""
    from .archive_service import ArchiveService

    service = ArchiveService()
    request = archive_request()
    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        pipeline = ArchivePipeline(provider(tmp_path, client), EncryptedLocalObjects(tmp_path/"o",b"x"*32))
        await pipeline.preflight(request)
        result, _ = await pipeline.submit(request)
        assert result["outcome"]["readback_verified"] is True
        assert len(service.seen) == 7  # 上传+三个对象的内容和锁
        assert (await pipeline.query(request))[0]["outcome"]["manifest_hash"] == result["outcome"]["manifest_hash"]
    assert len(service.seen) == 14
    assert [method for method,_ in service.seen].count("POST") == 1


@pytest.mark.parametrize("fault", ["missing", "hash", "lock", "hold", "seal", "aggregate_retention"])
async def test_provider_verified_flag_cannot_bypass_actual_readback(tmp_path: Any, fault: str) -> None:
    """远端即使提供完整succeeded和verified也不能掩盖材料/锁错误。"""
    from xuanpu_finance.settlement.provider import ProviderResponseError

    from .archive_service import ArchiveService

    service = ArchiveService(fault)
    request = archive_request()
    async with httpx.AsyncClient(transport=httpx.MockTransport(service)) as client:
        pipeline = ArchivePipeline(provider(tmp_path, client), EncryptedLocalObjects(tmp_path/"o",b"x"*32))
        await pipeline.preflight(request)
        with pytest.raises(ProviderResponseError) as caught:
            await pipeline.submit(request)
        assert b'"status":"succeeded"' in caught.value.raw.replace(b" ", b"")
        if fault != "aggregate_retention":
            assert caught.value.readback_evidence
        with pytest.raises(ProviderResponseError):
            await pipeline.query(request)
    assert [method for method,_ in service.seen].count("POST") == 1


async def test_cached_preflight_recovers_without_refetch_and_preserves_check_time(tmp_path: Any) -> None:
    """首次物化后的重启复用加密副本，真实检测时间不等同事实截点。"""
    objects = EncryptedLocalObjects(tmp_path/"objects", b"x"*32)
    request = archive_request()
    request["payload"]["archive_snapshot"]["event_cutoff"] = "2020-01-01T00:00:00+00:00"
    snapshot = request["payload"]["archive_snapshot"]
    snapshot["source_snapshot_hash"] = request_digest({k:v for k,v in snapshot.items() if k != "source_snapshot_hash"})
    request["payload_hash"] = request_digest({k:v for k,v in request.items() if k != "payload_hash"})
    async with httpx.AsyncClient() as client:
        pipeline = ArchivePipeline(provider(tmp_path,client),objects)
        descriptor = await pipeline.preflight(request)
        assert descriptor["four_qualities"][0]["checked_at"] != snapshot["event_cutoff"]
        assert descriptor["manifest"]["snapshots"] == snapshot["snapshots"]
        assert descriptor["manifest"]["event_cutoff"] == snapshot["event_cutoff"]
        restarted = ArchivePipeline(provider(tmp_path,client),objects)
        assert await restarted.preflight(request) == descriptor


async def test_actual_tcp_archive_and_restarted_query(tmp_path: Any) -> None:
    """真实回环HTTP上传ZIP，重建客户端后仍按原号逐版本查单。"""
    import asyncio

    from .archive_service import ArchiveService

    service = ArchiveService()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """把实际HTTP原文交给隔离档案协议服务。"""
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method, path, _ = lines[0].split(" ")
        headers = dict(line.split(":",1) for line in lines[1:] if ":" in line)
        headers = {k.strip().lower():v.strip() for k,v in headers.items()}
        raw = await reader.readexactly(int(headers.get("content-length","0")))
        response = service(httpx.Request(method,"http://127.0.0.1"+path,headers=headers,content=raw))
        body = response.content
        writer.write(b"HTTP/1.1 "+str(response.status_code).encode()+b" OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\nContent-Type: "+response.headers.get("content-type","application/octet-stream").encode()+b"\r\n\r\n"+body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle,"127.0.0.1",0)
    port = server.sockets[0].getsockname()[1]
    request = archive_request()
    objects = EncryptedLocalObjects(tmp_path/"o",b"x"*32)
    try:
        async with httpx.AsyncClient(trust_env=False,timeout=5) as client:
            registry = provider(tmp_path,client)
            registry.configs[("test-archive",1)].base_url = f"http://127.0.0.1:{port}"
            first = ArchivePipeline(registry,objects)
            await first.preflight(request)
            result, _ = await first.submit(request)
        async with httpx.AsyncClient(trust_env=False,timeout=5) as client:
            registry = provider(tmp_path,client)
            registry.configs[("test-archive",1)].base_url = f"http://127.0.0.1:{port}"
            second = ArchivePipeline(registry,objects)
            restored, _ = await second.query(request)
            assert restored["outcome"] == result["outcome"]
    finally:
        server.close()
        await server.wait_closed()
    assert len(service.seen) == 14
    assert [method for method,_ in service.seen].count("POST") == 1


@pytest.mark.parametrize("partial", [False, True])
async def test_readback_network_failure_keeps_main_and_partial_bytes(tmp_path: Any, partial: bool) -> None:
    """对象读取立即断网或中途断流，不能丢掉先前真实封存回执。"""
    from xuanpu_finance.settlement.provider import ProviderResponseError

    from .archive_service import ArchiveService

    class Interrupted(httpx.AsyncByteStream):
        """故障注入只作用于实际对象读回。"""

        async def __aiter__(self):
            """有前缀时也保全真实收到的字节。"""
            if partial:
                yield b"received-prefix"
            raise httpx.ReadError("isolated test failure")

    service = ArchiveService()

    def handle(req: httpx.Request) -> httpx.Response:
        """先成功接收封存包，再在读回端断流。"""
        if "/objects/" in req.url.path:
            return httpx.Response(200, stream=Interrupted(), headers={"content-type":"application/json"})
        return service(req)

    request = archive_request()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        pipeline = ArchivePipeline(provider(tmp_path,client),EncryptedLocalObjects(tmp_path/"o",b"x"*32))
        await pipeline.preflight(request)
        with pytest.raises(ProviderResponseError) as caught:
            await pipeline.submit(request)
    assert b'"status":"succeeded"' in caught.value.raw.replace(b" ",b"")
    assert caught.value.readback_evidence
    if partial:
        envelope = base64.b64decode(caught.value.readback_evidence[0]["content_base64"])
        assert base64.b64encode(b"received-prefix") in envelope


async def test_empty_error_body_is_preserved_as_zero_byte_raw_evidence(tmp_path: Any) -> None:
    """HTTP空错误体也是实际收到的字节，不能使后续档案永久不可封存。"""
    request = archive_request()
    snapshot = request["payload"]["archive_snapshot"]
    snapshot["artifacts"].append(dict(id="empty-receipt",path="receipts/empty.bin",kind="tax_receipt",
        sha256=hashlib.sha256(b"").hexdigest(),media_type="application/octet-stream",size=0,
        source={"content_base64":""}))
    snapshot["source_snapshot_hash"] = request_digest({k:v for k,v in snapshot.items() if k!="source_snapshot_hash"})
    request["payload_hash"] = request_digest({k:v for k,v in request.items() if k!="payload_hash"})
    async with httpx.AsyncClient() as client:
        pipeline = ArchivePipeline(provider(tmp_path,client),EncryptedLocalObjects(tmp_path/"o",b"x"*32))
        descriptor = await pipeline.preflight(request)
    assert next(m for m in descriptor["manifest"]["members"] if m["id"]=="empty-receipt")["size"] == 0
