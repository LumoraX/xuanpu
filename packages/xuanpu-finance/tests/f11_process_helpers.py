"""F11真实CLI生命周期、签名真人审批与进程退出取证。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import signal
import socket
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import jwt

from .test_f7_process import call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from .test_f0_e2e import SignedClient


@asynccontextmanager
async def observed_process(path: Path, outcomes: list[dict[str, Any]]) -> AsyncIterator[None]:
    """启动同一产品CLI，SIGTERM优雅停止并保存真实退出及关闭日志证据。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'xuanpu_finance', '--config', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield
    finally:
        if process.returncode is None:
            process.terminate()
        stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
        result = {'exit': process.returncode, 'ended': process.returncode is not None,
                  'graceful_shutdown': b'Application shutdown complete.' in stderr,
                  'stdout_empty': stdout == b''}
        outcomes.append(result)
        assert process.returncode in (0, -signal.SIGTERM), result
        assert result['graceful_shutdown'] and result['stdout_empty'], result


def port_closed(port: int) -> bool:
    """实际TCP连接失败才能证明loopback监听已经释放。"""
    with socket.socket() as channel:
        channel.settimeout(.2)
        return channel.connect_ex(('127.0.0.1', port)) != 0


async def read_original(signed: SignedClient, prefix: str, doc: dict[str, Any],
                        user: str, raw: bytes) -> dict[str, Any]:
    """真人逐级读取实际收件回执所关联原件并核对原始字节摘要。"""
    material = await call(signed, 'GET', prefix+'/materials/'+doc['content']['evidence_ids'][0], user=user)
    received = base64.b64decode(material['content_base64'])
    assert received == raw
    return {'reader': user, 'sha256': hashlib.sha256(received).hexdigest(),
            'bytes_equal': received == raw}


async def approve(signed: SignedClient, prefix: str, user: str,
                  raw: bytes, *, reject_model: bool) -> dict[str, Any]:
    """有效人工批准计划先拒绝模型签名执行，之后同一计划由真人确认执行。"""
    doc = await call(signed, 'GET', prefix, user=user)
    original = await read_original(signed, prefix, doc, user, raw)
    reason = user+'已核对独立原件'
    plan = await call(signed, 'POST', '/v1/finance/action-previews', user=user, idem='preview-'+user,
                      body={'action': 'approve', 'target_type': 'expense_claim', 'target_id': doc['id'],
                            'expected_version': doc['version'], 'parameters': {
                                'workflow_step_id': doc['workflow']['current_step_id'], 'reason': reason}})
    assert plan['executable'], plan['blockers']
    execution = {'plan_id': plan['id'], 'plan_digest': plan['digest']}
    rejected: dict[str, Any] = {}
    if reject_model:
        response = await signed.call('POST', '/v1/finance/action-executions', user=user, origin='agent',
                                     body=execution, idem='model-execute-'+user)
        after = await call(signed, 'GET', prefix, user=user)
        rejected = {'status': response.status_code, 'output': response.json(), 'signed_origin': signed_origin(response, signed),
                    'document_unchanged': after == doc}
        assert response.status_code == 403 and response.json()['code'] == 'HUMAN_REQUIRED'
        assert rejected['document_unchanged'] and rejected['signed_origin'] == 'agent'
    human = await signed.call('POST', '/v1/finance/action-executions', user=user, origin='human',
                              body=execution, idem='human-execute-'+user)
    assert human.is_success, human.text
    assert signed_origin(human, signed) == 'human'
    return {'original': original, 'model_rejection': rejected, 'human_execution': human.json(),
            'signed_origin': signed_origin(human, signed), 'reason': reason}


async def rejected_startup(path: Path) -> tuple[int, dict[str, Any], str]:
    """真实正式CLI坏权限仅返回固定JSON，不接管或替换任何启动边界。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'xuanpu_finance', '--config', str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 15)
    assert process.returncode is not None
    return process.returncode, json.loads(stdout), stderr.decode()


def signed_origin(response: httpx.Response, signed: SignedClient) -> str:
    """从实际HTTP请求中验证EdDSA签名并读取来源，证据不填设定常数。"""
    claims = jwt.decode(response.request.headers['X-Xuanpu-Context'], signed.key.public_key(),
                        algorithms=['EdDSA'], audience='finance', issuer='platform')
    return str(claims['origin'])
