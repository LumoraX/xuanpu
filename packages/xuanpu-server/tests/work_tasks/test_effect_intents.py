"""效应意图、失效epoch和写成功丢回执的真实HTTP边界。"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.runtime.effect_store import EffectStore
from xuanpu_server.work_tasks.execution_context import ExecutionBinding

from .test_execution_store import prepared


async def test_intent_committed_before_network_and_cannot_reissue(task_env):
    """意图即屏障；改call_id也不能通过整轮重算恢复。"""
    tasks, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='p', now=1000.)
    effects = EffectStore(ExecutionBinding(lease, execution, lambda:1000.))
    receipt = await effects.begin(call_id='c1', tool='api__crm__write', arguments_digest='c'*64)
    assert (await tasks.get(task_env.who, task.task_id)).unverified_write
    with pytest.raises(ConflictError):
        await effects.begin(call_id='c1', tool='api__crm__write', arguments_digest='c'*64)
    with pytest.raises(ConflictError):
        await effects.begin(call_id='changed-call',tool='api__crm__write',arguments_digest='c'*64)
    await effects.confirm(receipt, '{"remote_id":"T1"}', authority='controlled-http-v1')
    assert not (await tasks.get(task_env.who, task.task_id)).unverified_write


async def test_expired_intent_owner_cannot_confirm_or_dispatch(task_env):
    """旧请求迟到成功不能推进下一阶段或覆盖新epoch。"""
    tasks, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='old', now=1000.)
    binding = ExecutionBinding(lease, execution, lambda:1000.)
    effects = EffectStore(binding)
    receipt = await effects.begin(call_id='c1', tool='api__crm__write', arguments_digest='c'*64)
    binding.now = lambda:1061.
    with pytest.raises(ConflictError, match='stale_epoch'):
        await effects.confirm(receipt, 'ok', authority='controlled-http-v1')
    with pytest.raises(ConflictError, match='stale_epoch'):
        await effects.begin(call_id='c2', tool='api__crm__write', arguments_digest='c'*64)
    assert (await tasks.get(task_env.who, task.task_id)).unverified_write


async def test_real_http_success_lost_reply_keeps_unknown_barrier(task_env):
    """独立HTTP接收端已应用，断开响应后平台仍保留未知意图且不补发。"""
    tasks, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='p', now=1000.)
    effects = EffectStore(ExecutionBinding(lease, execution, lambda:1000.))
    applied = []
    async def remote(reader, writer):
        await reader.readuntil(b'\r\n\r\n')
        applied.append('write')
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(remote, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    async with server, httpx.AsyncClient(trust_env=False) as client:
        await effects.begin(call_id='c1', tool='api__crm__write', arguments_digest='c'*64)
        with pytest.raises(httpx.RemoteProtocolError):
            await client.post(f'http://127.0.0.1:{port}/write')
        with pytest.raises(ConflictError):
            await effects.begin(call_id='c1', tool='api__crm__write', arguments_digest='c'*64)
    assert len(applied) == 1
    assert (await tasks.get(task_env.who, task.task_id)).unverified_write
