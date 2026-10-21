"""独立真实HTTP侧车丢回执后，持久去重和权威查询跨进程重启不丢。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest


async def start_counter(database):
    """端口从真实监听返回，进程日志不打印任何认证信息。"""
    script=Path(__file__).resolve().parents[4]/'scripts/acceptance/r2_effect_server.py'
    process=await asyncio.create_subprocess_exec(sys.executable,str(script),'--db',str(database),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    async with asyncio.timeout(10):
        line=await process.stdout.readline()
    if not line:
        _,error=await process.communicate()
        raise AssertionError(error.decode())
    port=json.loads(line)['port']
    return process,'http://127.0.0.1:'+str(port)


async def test_drop_reply_then_sigkill_counter_keeps_one_write(tmp_path):
    """不是MockTransport：实际TCP写入后断线，杀进程重建仍只有一次应用。"""
    database=tmp_path/'effects.sqlite'
    process,url=await start_counter(database)
    payload={'stable_key':'same-logical-action','payload':{'synthetic_amount':'15.50'}}
    try:
        async with httpx.AsyncClient(base_url=url,trust_env=False) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.post('/effects',json=payload,headers={'X-Test-Reply':'drop'})
            assert (await client.get('/count')).json()['applied_count']==1
        process.kill();await process.wait()
        process,url=await start_counter(database)
        async with httpx.AsyncClient(base_url=url,trust_env=False) as client:
            response=await client.post('/effects',json=payload)
            assert response.status_code==200 and response.json()['state']=='applied'
            query=(await client.get('/effects/same-logical-action')).json()
            assert query['applied_count']==1 and query['receipt']['result_digest']==response.json()['result_digest']
            conflict=await client.post('/effects',json={**payload,'payload':{'synthetic_amount':'99.00'}})
            assert conflict.status_code==409
            assert (await client.get('/count')).json()['applied_count']==1
    finally:
        if process.returncode is None:process.terminate();await process.wait()
