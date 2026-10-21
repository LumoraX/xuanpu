"""只在回环运行的税务与档案测试服务，不接触真实供应商。"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import httpx
from xuanpu_finance.settlement.provider import RESULT_SCOPE

from .provider.archive_service import ArchiveService
from .test_f3_domain import fresh_snapshot

if TYPE_CHECKING:
    import asyncio


class ComplianceService:
    """同时提供实际税务请求、证据文件和版本档案协议。"""

    def __init__(self) -> None:
        """建立各原请求号的持久模拟结果。"""
        self.requests: dict[str, dict[str, Any]] = {}
        self.archives: dict[str, ArchiveService] = {}
        self.revision = 0
        self.malformed_next = False
        self.red_cases: list[dict[str, Any]] = []
        self.adjustments: list[dict[str, Any]] = []
        self.seen: list[tuple[str, str]] = []
        self.tax_facts: dict[str, dict[str, Any]] = {}
        self.file = b'{"simulation":true,"evidence":"tax-receipt"}'

    def __call__(self, req: httpx.Request) -> httpx.Response:
        """所有写操作按固定请求号保存，查单和证据始终GET。"""
        self.seen.append((req.method, req.url.path))
        if req.url.path.startswith("/v1/archives"):
            return self.archive(req)
        if "/evidence/" in req.url.path:
            return httpx.Response(200,content=self.file,headers={"content-type":"application/json"})
        if req.method == "GET":
            return httpx.Response(200,json=self.requests[req.url.path.rsplit("/",1)[-1]])
        request = json.loads(req.content)
        self.revision += 1
        is_red = request["payload"]["target_snapshot"].get("data", {}).get("is_red", False)
        snapshot = fresh_snapshot(self.revision,red_letter_cases=[] if is_red else self.red_cases,
            adjustments=[] if is_red else self.adjustments)
        invoice_id = request["target"]["id"]
        snapshot.update(self.tax_facts.get(invoice_id, {}))
        params = request["payload"]["parameters"]
        if request["action"] in {"confirm_usage","confirm_tax_entry"}:
            snapshot.update(accounting_period=params["accounting_period"])
            if request["action"] == "confirm_usage":
                snapshot.update(usage_status="confirmed",usage_kind="vat_deduction",usage_confirmation_id="usage1")
            else:
                snapshot.update(tax_entry_status="confirmed",tax_entry_confirmation_id="entry1")
        if request["action"] in {"confirm_usage","confirm_tax_entry"}:
            self.tax_facts[invoice_id] = {k:snapshot.get(k) for k in (
                "usage_status","usage_kind","usage_confirmation_id","tax_entry_status",
                "tax_entry_confirmation_id","accounting_period")}
        if request["action"] == "confirm_red_letter":
            snapshot["red_letter_cases"] = [{**case,"status":"confirmed","authority_revision":self.revision}
                                           for case in self.red_cases]
        result = {**{key:request[key] for key in RESULT_SCOPE},"status":"succeeded","outcome":snapshot,
            "evidence":[dict(id="tax-file",sha256=hashlib.sha256(self.file).hexdigest(),
                             media_type="application/json",size=len(self.file),object_ref="simulation-evidence")]}
        self.requests[request["external_request_id"]] = result
        if self.malformed_next:
            self.malformed_next = False
            return httpx.Response(200,content=b'{"status":',headers={"content-type":"application/json"})
        return httpx.Response(200,json=result)

    def archive(self, req: httpx.Request) -> httpx.Response:
        """每份包独立保存，使旧父包可在补包之后继续读回。"""
        if req.method == "POST":
            service = ArchiveService()
            response = service(req)
            self.archives[service.result["external_request_id"]] = service
            return response
        request_id = req.url.path.split("/")[3]
        return self.archives[request_id](req)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """读取真实TCP HTTP消息，服务无跨请求数据库依赖。"""
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        method,path,_ = lines[0].split(" ")
        headers = dict(line.split(":",1) for line in lines[1:] if ":" in line)
        headers = {k.strip().lower():v.strip() for k,v in headers.items()}
        content = await reader.readexactly(int(headers.get("content-length","0")))
        response = self(httpx.Request(method,"http://127.0.0.1"+path,headers=headers,content=content))
        body = response.content
        writer.write(b"HTTP/1.1 "+str(response.status_code).encode()+b" OK\r\nConnection: close\r\nContent-Length: "+str(len(body)).encode()+b"\r\nContent-Type: "+response.headers.get("content-type","application/json").encode()+b"\r\n\r\n"+body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
