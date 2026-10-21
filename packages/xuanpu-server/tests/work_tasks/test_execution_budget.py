"""真实双后端预算、未知占用与结算幂等回归。"""
from __future__ import annotations

from dataclasses import replace

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.quota import usage_day
from xuanpu_server.tenancy import QuotaLimits
from xuanpu_server.work_tasks.budget_store import BudgetStore

from .test_execution_store import prepared


async def test_unknown_usage_keeps_full_reservation_across_attempt(task_env):
    """旧请求完整占用不退款，只在剩余额度足够时才允许新请求。"""
    tasks, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='old', now=1000.)
    budget = BudgetStore(execution, max_task_tokens=100)
    await budget.reserve(lease, 'unknown', 60, now=1000.)
    await tasks.resources_closed(await tasks.get(task_env.who, task.task_id), at=1061.)
    new = await execution.claim(task_env.who, task.task_id, owner='new', now=1061.)
    await budget.reserve(new, 'next', 30, now=1061.)
    with pytest.raises(ConflictError, match='block_budget'):
        await budget.reserve(new, 'over', 11, now=1061.)
    usage = await task_env.control.get_usage('acme', usage_day(1000.))
    assert usage.total_tokens == 90


async def test_settlement_atomic_once_and_original_usage_day(task_env):
    """真实用量调整一次；跨UTC日结算仍归原预留日。"""
    _, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='one', now=1000.)
    budget = BudgetStore(execution)
    await budget.reserve(lease, 'one', 100, now=1000.)
    await budget.settle(lease, 'one', input_tokens=10, output_tokens=5, now=1001.)
    await budget.settle(lease, 'one', input_tokens=10, output_tokens=5, now=1001.)
    usage = await task_env.control.get_usage('acme', usage_day(1000.))
    employee = await task_env.identity.list_user_usage('acme', since=usage.day, until=usage.day, user_id='usr_a')
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens, usage.turns) == (10,5,15,1)
    assert employee[0].total_tokens == 15
    with pytest.raises(ConflictError):
        await budget.settle(lease, 'one', input_tokens=11, output_tokens=5, now=1001.)


async def test_tenant_limit_counts_unknown_other_employee(task_env):
    """员工切换不能借新任务绕开租户已有保守预留。"""
    env = task_env
    tenant = await env.control.get_tenant('acme')
    await env.control.update_tenant(replace(tenant, quota=QuotaLimits(max_daily_tokens=100)))
    tasks, execution, task = await prepared(env)
    lease = await execution.claim(env.who, task.task_id, owner='p', now=1000.)
    budget = BudgetStore(execution)
    await budget.reserve(lease, 'one', 80, now=1000.)
    with pytest.raises(ConflictError, match='block_budget'):
        await budget.reserve(lease, 'two', 21, now=1000.)


async def test_budget_ledger_failure_prevents_model_dispatch(task_env, monkeypatch):
    """预留事务读失败时，实际模型请求数必须仍为零。"""
    import taifeng
    from xuanpu_server.runtime.task_model_budget import TaskModelClient
    from xuanpu_server.work_tasks.execution_context import ExecutionBinding

    _, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='p', now=1000.)
    calls = []
    class Inner:
        """请求边界计数，不能拿模型工厂构造当作实际请求。"""
        def session(self, **kwargs):
            return self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return
        async def stream(self, request):
            calls.append(request)
            yield None
    async def broken(*args):
        raise OSError('账本不可读')
    monkeypatch.setattr(task_env.control, 'get_usage', broken)
    client = TaskModelClient(Inner(), ExecutionBinding(lease, execution, lambda:1000.), verified_text_bound=True)
    with pytest.raises(OSError):
        async with client.session(cancel=taifeng.CancellationToken()) as session:
            async for _ in session.stream(taifeng.ApiRequest(model='text')):
                pass
    assert calls == []


async def test_bound_accepts_plaintext_field_names_but_rejects_actual_multimodal():
    """纯文本可讨论image_url，但真正图片内容必须阻塞。"""
    from xuanpu_server.runtime.task_model_budget import text_request_bound

    assert text_request_bound({'messages':[{'content':'请解释image_url字段'}]}) > 4096
    with pytest.raises(ConflictError,match='输入类型'):
        text_request_bound({'messages':[{'content':[{'type':'image_url','image_url':'https://example.invalid/image'}]}]})
    with pytest.raises(ConflictError,match='输入项'):
        text_request_bound({'input_items':[{'type':'reasoning','encrypted_content':'opaque'}]})


async def test_standalone_grant_budget_is_shared_by_multiple_tasks(task_env):
    """授权先于任务存在；新任务不能重新获得同一grant的全部额度。"""
    from xuanpu_server.work_tasks.execution_models import ExecutionGrant
    from xuanpu_server.work_tasks.execution_store import ExecutionStore
    from xuanpu_server.work_tasks.store import WorkTaskStore

    env = task_env
    tasks,execution = WorkTaskStore(env.workspace),ExecutionStore(env.workspace)
    grant = ExecutionGrant(grant_id='shared',tenant_id='acme',user_id='usr_a',actor=env.who.actor,
        role='tenant_member',package_digest=env.record.package_digest,
        input_digests=tuple(entry.sha256 for entry in env.record.inputs),created_at=1000.,expires_at=2000.,max_tokens=100)
    await execution.create_grant(env.who,grant)
    first,_ = await tasks.create_or_replay(env.who,env.record.model_copy(update={'recovery_policy_version':2,'grant_id':'shared'}))
    lease = await execution.claim(env.who,first.task_id,owner='p',now=1000.)
    budget = BudgetStore(execution,max_task_tokens=95)
    await budget.reserve(lease,'unknown-first',80,now=1000.)
    await tasks.resources_closed(await tasks.get(env.who,first.task_id),at=1001.)
    await execution.release(lease,state_name='waiting_user',now=1001.)
    second,_ = await tasks.create_or_replay(env.who,env.record.model_copy(update={
        'task_id':'second','request_id':'second','recovery_policy_version':2,'grant_id':'shared'}))
    next_lease = await execution.claim(env.who,second.task_id,owner='p',now=1001.)
    with pytest.raises(ConflictError,match='block_budget'):
        await budget.reserve(next_lease,'second',21,now=1001.)
    await budget.reserve(next_lease,'second-allowed',20,now=1001.)


async def test_each_model_request_has_text_limits_and_independent_usage(task_env):
    """同一模型会话的两次采样均预留并核销，根聚合不参与任务记账。"""
    import taifeng
    from xuanpu_server.runtime.task_model_budget import TaskModelClient
    from xuanpu_server.work_tasks.execution_context import ExecutionBinding

    _, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who, task.task_id, owner='p', now=1000.)
    calls = []
    class Inner:
        """模拟公开模型协议，断言实际请求的服务端限制。"""
        def session(self, **kwargs):
            return self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return
        async def stream(self, request):
            calls.append(request)
            yield taifeng.ResponseEvent(kind='completed',data={'usage':{'input_tokens':10,'output_tokens':5}})
    client = TaskModelClient(Inner(), ExecutionBinding(lease,execution,lambda:1000.),verified_text_bound=True)
    async with client.session(cancel=taifeng.CancellationToken()) as session:
        for _ in range(2):
            async for _ in session.stream(taifeng.ApiRequest(model='text',max_output_tokens=9999)):
                pass
    assert len(calls) == 2
    assert all(request.reasoning_effort == 'none' and request.max_output_tokens == 4096 for request in calls)
    usage = await task_env.control.get_usage('acme',usage_day(1000.))
    assert (usage.input_tokens,usage.output_tokens,usage.total_tokens,usage.turns) == (20,10,30,2)


@pytest.mark.parametrize('message',[
    {'role':'assistant','content':'ok','reasoning_state':{'anthropic':{'blocks':[{'type':'redacted_thinking','data':'opaque'}]}}},
    {'role':'assistant','content':'ok','tool_calls':[{'id':'c','type':'function',
      'function':{'name':'tool','arguments':'{}'},'extra_content':{'google':{'thought_signature':'opaque'}}}]},
])
async def test_legacy_provider_state_is_rejected_before_budget_estimation(message):
    """兼容messages中的签名状态不能绕过canonical provider_state的阻塞边界。"""
    import taifeng
    from xuanpu_server.runtime.task_model_budget import text_request_bound

    request = taifeng.ApiRequest(model='text',messages=[taifeng.ApiMessage(**message)],reasoning_effort='none')
    with pytest.raises(ConflictError,match='block_budget'):
        text_request_bound(request.model_dump(mode='json'))
