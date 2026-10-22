"""内部记录profile真实CLI跨进程恢复审批与离线预检。"""
from __future__ import annotations

import asyncio
import base64
import os
import sys
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from xuanpu_finance.config import FinanceConfig, GrantConfig
from xuanpu_finance.expenses.models import ExpensePolicy

from .test_f0_e2e import SignedClient
from .test_f5_archive_formats import actual_pdf
from .test_f7_process import action, call, receive_and_submit, record_gateway
from .test_f9_preflight import internal_config
from .test_process import finance_process, process_config, wait_for_ready
from .test_storage import WHO

if TYPE_CHECKING:
    from pathlib import Path

    from xuanpu_finance.storage import FinanceStore


async def test_internal_real_cli_two_startups_continue_human_approval(store: FinanceStore,tmp_path: Path) -> None:
    """真实CLI重启保留profile与待审，原平台删除原文后仍可人工核对批准。"""
    schema=await store.pool.fetchval('SELECT current_schema()')
    parts=urlsplit(os.environ['FINANCE_TEST_DSN'])
    dsn=urlunsplit(parts._replace(query=urlencode(parse_qsl(parts.query)+[('search_path',schema)])))
    raw,key=actual_pdf(),Ed25519PrivateKey.generate();payloads={'pdf':raw}
    async with record_gateway(payloads) as gateway:
        path,config=process_config(tmp_path/'process',dsn,gateway,key)
        config.profile='internal_records';config.synthetic=False
        config.grants.append(GrantConfig(tenant_id=WHO.tenant_id,user_id='approver',legal_entity_id='company-a',role='reviewer'))
        config.expense_policies=[ExpensePolicy(tenant_id=WHO.tenant_id,legal_entity_id='company-a',version=1,enabled=True,
            record_mode='internal_record',require_verified_invoice=False,categories=[{'id':'travel','name':'交通'}],cost_centers=[],
            steps=[{'id':'manager','name':'负责人','approver_user_id':'approver'}],exception_owner_user_id='approver')]
        config=FinanceConfig.model_validate(config.model_dump())
        path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{config.port}') as http:
            signed=SignedClient(http,key)
            async with finance_process(path):
                await wait_for_ready(http)
                caps=await call(signed,'GET','/v1/finance/capabilities')
                assert caps['profile']=='internal_records' and caps['internal_records_only']
                doc=await receive_and_submit(signed,raw)
            payloads.clear()
            prefix='/v1/finance/expense-claims/'+doc['id']
            async with finance_process(path):
                await wait_for_ready(http)
                assert (await call(signed,'GET','/v1/finance/capabilities'))['profile']=='internal_records'
                summary=await call(signed,'GET','/v1/finance/expense-claim-summaries',params={'legal_entity_id':'company-a'})
                assert summary['total']==1 and summary['items'][0]['status']=='under_review'
                doc=await call(signed,'GET',prefix,user='approver')
                material=await call(signed,'GET',prefix+'/materials/'+doc['content']['evidence_ids'][0],user='approver')
                assert base64.b64decode(material['content_base64'])==raw
                await action(signed,doc,'approve','approver',workflow_step_id=doc['workflow']['current_step_id'],reason='隔离试用原件核对')
                assert (await call(signed,'GET',prefix))['status']=='approved'
                assert (await signed.call('POST','/v1/finance/invoice-imports')).status_code==404
    assert await store.pool.fetchval("SELECT status FROM finance_expense_claim WHERE id=$1",doc['id'])=='approved'
    for table in ['finance_invoice','finance_settlement_operation','finance_compliance_operation']:
        assert await store.pool.fetchval('SELECT count(*) FROM '+table)==0


async def cli_preflight(path: Path) -> tuple[int,bytes]:
    """启动真正CLI离线检查并保留脱敏标准输出。"""
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','xuanpu_finance',
        '--config',str(path),'--preflight',stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    output,error=await asyncio.wait_for(process.communicate(),10)
    assert not error,error.decode()
    assert process.returncode is not None
    return process.returncode,output


async def test_internal_cli_offline_pass_and_unfilled_blocked(tmp_path: Path) -> None:
    """合法隔离配置检查通过但正式未验，REPLACE_模板退出2且不暴露配置。"""
    import json

    config=internal_config(tmp_path);path=tmp_path/'config.yaml'
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    code,output=await cli_preflight(path)
    report=json.loads(output)
    assert code==0 and report['status']=='not_verified'
    assert all(c['status']=='passed' for c in report['checks'])
    config.companies[0].name='REPLACE_COMPANY'
    path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    code,output=await cli_preflight(path)
    assert code==2 and json.loads(output)['status']=='blocked'
    assert str(tmp_path).encode() not in output and b'REPLACE_' not in output
