"""真实CLI进程三次启动验证独立记录、真人待办及历史原件恢复。"""
from __future__ import annotations

import asyncio
import base64
import os
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request, Response
from xuanpu_finance.config import GrantConfig
from xuanpu_finance.expenses.models import ExpensePolicy

from .test_f0_e2e import SignedClient
from .test_f5_archive_formats import actual_pdf
from .test_process import finance_process, free_port, process_config, wait_for_ready
from .test_storage import WHO
from .test_worker import ref

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


@asynccontextmanager
async def record_gateway(payloads: dict[str,bytes]) -> AsyncIterator[str]:
    """仅替换平台在线身份与文件外部边界，不替换财务CLI/数据库/对象存储。"""
    app = FastAPI()

    @app.get('/v1/integration-gateway/identity')
    async def identity(request: Request) -> dict[str,Any]:
        """回显测试目录当前实际请求员工，非真实公司身份验收。"""
        return {'tenant_id':WHO.tenant_id,'user_id':request.headers['X-Xuanpu-Act-As'],'active':True}

    @app.get('/v1/integration-gateway/files/{id}')
    async def file(id: str) -> Response:
        """返回有效PDF，清空后原平台不再提供源字节。"""
        return Response(payloads[id],media_type='application/pdf')

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='critical',access_log=False))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(5):
            while not server.started:
                await asyncio.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        server.should_exit = True
        await task


async def call(signed: SignedClient,method: str,path: str,**kwargs: Any) -> dict[str,Any]:
    """真正签名绑定请求并检查真实HTTP响应，错误不吞掉。"""
    response = await signed.call(method,path,origin='human',**kwargs)
    assert response.is_success,response.text
    return response.json()


async def action(signed: SignedClient,doc: Any,name: str,user: str,**params: Any) -> None:
    """人工预检和人工确认拆成两个真正HTTP事务。"""
    body = {'action':name,'target_type':'expense_claim','target_id':doc['id'],
            'expected_version':doc['version'],'parameters':params}
    plan = await call(signed,'POST','/v1/finance/action-previews',user=user,body=body,idem='preview-'+name+str(doc['version']))
    assert plan['executable'],plan['blockers']
    await call(signed,'POST','/v1/finance/action-executions',user=user,
               body={'plan_id':plan['id'],'plan_digest':plan['digest']},idem='execute-'+plan['id'])


async def receive_and_submit(signed: SignedClient,raw: bytes) -> dict[str,Any]:
    """第一进程通过实际worker独立收件，并本人确认进入待审。"""
    op = await call(signed,'POST','/v1/finance/supporting-material-imports',idem='receipt',
                    body={'legal_entity_id':'company-a','files':[ref('pdf',raw).model_dump()]})
    async with asyncio.timeout(10):
        while True:
            received = await call(signed,'GET','/v1/finance/operations/'+op['id'])
            if received['status']=='succeeded':
                break
            await asyncio.sleep(.05)
    receipt = received['result_refs'][0]['id']
    doc = await call(signed,'POST','/v1/finance/expense-claims',idem='record',body={
        'legal_entity_id':'company-a','reason':'交通报销','invoice_information':'  票号TEST-ONLY\n金额10.00 ',
        'lines':[{'category_id':'travel','amount':'10.00','occurred_on':'2026-10-22','reason':'地铁'}],
        'evidence_ids':[receipt]})
    await action(signed,doc,'submit_claim',WHO.user_id)
    return doc


async def test_real_cli_restart_preserves_record_and_human_original(store: FinanceStore,tmp_path: Path) -> None:
    """待审跨进程重启、源平台字节移除后批准，第三次启动保留历史及零外部操作。"""
    schema = await store.pool.fetchval('SELECT current_schema()')
    parts = urlsplit(os.environ['FINANCE_TEST_DSN'])
    dsn = urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query)+[('search_path',schema)])))
    raw, key = actual_pdf(), Ed25519PrivateKey.generate()
    payloads = {'pdf':raw}
    async with record_gateway(payloads) as gateway:
        path, config = process_config(tmp_path/'process',dsn,gateway,key)
        config.synthetic = False
        config.grants.append(GrantConfig(tenant_id=WHO.tenant_id,user_id='approver',legal_entity_id='company-a',role='reviewer'))
        config.expense_policies = [ExpensePolicy(tenant_id=WHO.tenant_id,legal_entity_id='company-a',version=1,enabled=True,
            record_mode='internal_record',require_verified_invoice=False,categories=[{'id':'travel','name':'交通'}],cost_centers=[],
            steps=[{'id':'manager','name':'负责人','approver_user_id':'approver'}],exception_owner_user_id='approver')]
        path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}') as http:
            signed = SignedClient(http,key)
            async with finance_process(path):
                await wait_for_ready(http)
                doc = await receive_and_submit(signed,raw)
            payloads.clear()
            prefix = '/v1/finance/expense-claims/'+doc['id']
            async with finance_process(path):
                await wait_for_ready(http)
                doc = await call(signed,'GET',prefix,user='approver')
                assert doc['status']=='under_review'
                material = await call(signed,'GET',prefix+'/materials/'+doc['content']['evidence_ids'][0],user='approver')
                assert base64.b64decode(material['content_base64'])==raw
                await action(signed,doc,'approve','approver',workflow_step_id=doc['workflow']['current_step_id'],reason='已核对原件')
            async with finance_process(path):
                await wait_for_ready(http)
                done = await call(signed,'GET',prefix)
                history = await call(signed,'GET',prefix+'/history')
                assert done['status']=='approved' and done['content']['invoice_information']=='  票号TEST-ONLY\n金额10.00 '
                assert done['paid_amount']=='0.00' and done['posted_amount']=='0.00'
                assert any(item['kind']=='decision' and item['data']['parameters']['reason']=='已核对原件' for item in history['items'])
    for table in ['finance_invoice','finance_settlement_operation','finance_compliance_operation']:
        assert await store.pool.fetchval('SELECT count(*) FROM '+table)==0
