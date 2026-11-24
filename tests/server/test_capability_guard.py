"""能力依据的逐次复核（R4-B1）：执行空间解析、工具出口先复核再交给原策略、模型请求前复核。"""
from __future__ import annotations

import pytest
import taifeng

from server.core.errors import PermissionDeniedError
from server.engine.runtime.capability_guard import (
    CapabilityModelClient,
    CapabilityPermissionPolicy,
    capability_check,
)
from server.engine.runtime.execution_scope import ExecutionScope


async def test_scope_maps_to_binding_kind_and_ref() -> None:
    """session:/task:（含 attempt 后缀）对应会话与任务绑定；服务装配等其他空间不复核。"""
    seen = []

    async def admission(tenant, package, kind, ref_id):
        seen.append((tenant, package, kind, ref_id))

    for execution in ('session:ses_1', 'task:tsk_1:attempt:3'):
        check = capability_check(admission, ExecutionScope('acme', 'usr_a', execution), 'pkg')
        assert check is not None
        await check()
    assert seen == [('acme', 'pkg', 'session', 'ses_1'), ('acme', 'pkg', 'task', 'tsk_1')]
    assert capability_check(admission, ExecutionScope('acme', 'service:assembly', 'package:pkg'), 'pkg') is None
    assert capability_check(None, ExecutionScope('acme', 'usr_a', 'session:ses_1'), 'pkg') is None


class _Inner(taifeng.PermissionPolicy):
    """记录是否被调用的原策略。"""

    def __init__(self) -> None:
        super().__init__(rules=[], default_mode='allow')
        self.calls = 0

    async def check(self, request):
        self.calls += 1
        return await super().check(request)


async def test_tool_exit_rechecks_before_original_policy() -> None:
    """资格失效：工具调用不执行、原策略也不被问到；资格有效：判定仍由原策略做。"""
    state = {'ok': True}

    async def check():
        if not state['ok']:
            raise PermissionDeniedError('能力分配已撤销或过期，这项工作不能继续')

    inner = _Inner()
    policy = CapabilityPermissionPolicy(inner, check)
    request = taifeng.PermissionRequest(scope='tool_use', target='any_tool')
    assert (await policy.check(request)).granted is True and inner.calls == 1
    state['ok'] = False
    with pytest.raises(PermissionDeniedError):
        await policy.check(request)
    assert inner.calls == 1


class _Session:
    """记录请求的底层会话。"""

    def __init__(self, sent):
        self.sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def stream(self, request):
        self.sent.append(request)
        yield 'event'


class _Client:
    """底层模型客户端。"""

    def __init__(self):
        self.sent = []

    def session(self, *, cancel, model=None):
        return _Session(self.sent)


async def test_model_request_rechecked_before_each_send() -> None:
    """资格失效后下一次请求不发出；之前已发出的不受影响。"""
    state = {'ok': True}

    async def check():
        if not state['ok']:
            raise PermissionDeniedError('撤销')

    inner = _Client()
    client = CapabilityModelClient(inner, check)
    request = taifeng.ApiRequest(model='m')
    async with client.session(cancel=taifeng.CancellationToken()) as session:
        assert [e async for e in session.stream(request)] == ['event']
        state['ok'] = False
        with pytest.raises(PermissionDeniedError):
            [e async for e in session.stream(request)]
    assert len(inner.sent) == 1


async def test_approval_on_wrapper_reaches_original_policy() -> None:
    """内核批准恢复时对包装调用 preapprove：原策略据此放行这一次调用（否则批准后的工具会失败）。"""
    async def check():
        return None

    inner = taifeng.PermissionPolicy(rules=[], default_mode='ask')
    policy = CapabilityPermissionPolicy(inner, check)
    request = taifeng.PermissionRequest(scope='tool_use', target='mcp__crm__update', metadata={'call_id': 'call_1'})
    assert (await policy.check(request)).granted is False
    policy.preapprove('call_1')
    assert (await policy.check(request)).granted is True
