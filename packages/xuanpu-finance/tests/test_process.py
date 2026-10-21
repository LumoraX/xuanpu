"""真实服务进程重启验证待补件和独立原件持久性。"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import uvicorn
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Response
from xuanpu_finance.config import GrantConfig
from xuanpu_finance.objects import EncryptedLocalObjects

from .test_f0_e2e import SignedClient
from .test_lifecycle import config_for
from .test_parser import SAMPLE
from .test_storage import WHO
from .test_worker import ref

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_finance.config import FinanceConfig
    from xuanpu_finance.storage import FinanceStore


def free_port() -> int:
    """只为本机隔离测试选择空闲端口。"""
    with socket.socket() as channel:
        channel.bind(("127.0.0.1", 0))
        return int(channel.getsockname()[1])


@asynccontextmanager
async def finance_process(config: Path) -> AsyncIterator[None]:
    """启动真正 CLI 进程，退出时停止并释放其数据库持有锁。"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "xuanpu_finance",
        "--config",
        str(config),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield
    finally:
        if child.returncode is None:
            child.terminate()
        await asyncio.wait_for(child.wait(), 10)


async def wait_for_ready(client: httpx.AsyncClient) -> None:
    """等待真实 HTTP listener，就绪失败不会冒充启动成功。"""
    async with asyncio.timeout(10):
        while True:
            try:
                if (await client.get("/health")).status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(0.05)


@asynccontextmanager
async def platform_stub(payloads: dict[str, bytes]) -> AsyncIterator[str]:
    """仅替换外部平台网络服务，财务进程/网关/数据库均使用实际实现。"""
    app = FastAPI()

    @app.get("/v1/integration-gateway/identity")
    async def identity() -> dict[str, Any]:
        """显式测试员工目录，不声称真实平台授权验收。"""
        return {"tenant_id": WHO.tenant_id, "user_id": WHO.user_id, "active": True}

    @app.get("/v1/integration-gateway/files/{id}")
    async def file(id: str) -> Response:
        """模拟平台侧原件；测试中会清除源字节。"""
        return Response(payloads[id], media_type="application/xml")

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def process_config(
    directory: Path, dsn: str, gateway: str, key: Ed25519PrivateKey
) -> tuple[Path, FinanceConfig]:
    """只在临时目录生成实际 CLI 配置及一次性公钥。"""
    config = config_for(directory, dsn)
    config.public_key_files = {"test": directory / "public.pem"}
    (directory / "public.pem").write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    (directory / "token").write_text("test-service")
    config.port, config.mode, config.synthetic = free_port(), "enabled", True
    config.gateway.base_url = gateway
    config.gateway.tenant_token_files = {WHO.tenant_id: directory / "gateway-token"}
    config.companies = [
        type(config.companies[0])(
            tenant_id=WHO.tenant_id, legal_entity_id="company-a", name="测试公司", tax_id="TEST"
        )
    ]

    config.grants = [
        GrantConfig(
            tenant_id=WHO.tenant_id,
            user_id=WHO.user_id,
            legal_entity_id="company-a",
            role="collector",
        )
    ]
    config_file = directory / "config.yaml"
    config_file.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    return config_file, config


async def test_real_process_restart_retains_hitl_and_original(
    store: FinanceStore, tmp_path: Path
) -> None:
    """停止进程后再启动仍能读待办并补件，删除源也不丢财务副本。"""
    parts = urlsplit(os.environ["FINANCE_TEST_DSN"])
    schema = await store.pool.fetchval("SELECT current_schema()")
    dsn = urlunsplit(
        parts._replace(query=urlencode(parse_qsl(parts.query) + [("search_path", schema)]))
    )
    pdf, key = b"%PDF-1.4 missing", Ed25519PrivateKey.generate()
    payloads = {"pdf": pdf, "xml": SAMPLE}
    async with platform_stub(payloads) as gateway:
        path, config = process_config(tmp_path / "process", dsn, gateway, key)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{config.port}") as client:
            signed = SignedClient(client, key)
            async with finance_process(path):
                await wait_for_ready(client)
                accepted = await signed.call(
                    "POST",
                    "/v1/finance/invoice-imports",
                    idem="process-first",
                    body={"legal_entity_id": "company-a", "files": [ref("pdf", pdf).model_dump()]},
                )
                assert accepted.status_code == 202, accepted.text
                async with asyncio.timeout(5):
                    while True:
                        response = await signed.call(
                            "GET", "/v1/finance/exceptions", params={"legal_entity_id": "company-a"}
                        )
                        if response.json()["total"]:
                            source = response.json()["items"][0]
                            break
                        await asyncio.sleep(0.05)
            async with finance_process(path):
                await wait_for_ready(client)
                response = await signed.call(
                    "GET", "/v1/finance/exceptions", params={"legal_entity_id": "company-a"}
                )
                assert response.json()["items"][0]["id"] == source["id"]
                accepted = await signed.call(
                    "POST",
                    "/v1/finance/invoice-imports",
                    idem="process-repair",
                    body={
                        "legal_entity_id": "company-a",
                        "files": [ref("xml", SAMPLE).model_dump()],
                        "source_exception_id": source["id"],
                        "expected_exception_version": source["version"],
                    },
                )
                assert accepted.status_code == 202, accepted.text
                async with asyncio.timeout(5):
                    while True:
                        result = await signed.call(
                            "GET", f"/v1/finance/operations/{accepted.json()['id']}"
                        )
                        if result.json()["status"] == "succeeded":
                            break
                        await asyncio.sleep(0.05)
                payloads.clear()
                objects = EncryptedLocalObjects(config.objects.root, b"k" * 32)
                receipts = await store.pool.fetch("SELECT object_key FROM finance_receipt")
                assert {await objects.get(row["object_key"]) for row in receipts} == {pdf, SAMPLE}
