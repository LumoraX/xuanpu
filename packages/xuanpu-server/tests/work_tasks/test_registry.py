"""后台托管持有执行引用，调用者离开不取消已受理任务。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from xuanpu_server.work_tasks.registry import TaskRegistry
from xuanpu_server.work_tasks.store import WorkTaskStore


async def test_registry_owns_execution_after_submitter_leaves(task_env):
    """独立调用者退出和重复start均不影响同一持久任务。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def run(who, task, decision=None):
        """保持任务在运行，模拟独立的模型处理。"""
        calls.append(task.task_id)
        entered.set()
        await release.wait()
        return {'status': 'waiting_user'}

    registry = TaskRegistry(store, SimpleNamespace(run=run), process_owner='p1', now=lambda: 1001.0)
    await registry.open(scan=False)
    try:
        async def submit():
            """请求只登记一次异步执行，返回后不拥有它。"""
            registry.start(env.who, env.record)
        await asyncio.create_task(submit())
        await asyncio.wait_for(entered.wait(), 5)
        registry.start(env.who, env.record)
        assert (await store.get(env.who, 'task1')).status == 'running'
        release.set()
        await registry.join()
        assert calls == ['task1']
        assert (await store.get(env.who, 'task1')).status == 'waiting_user'
    finally:
        await registry.close()


async def test_reconcile_never_replays_old_process(task_env):
    """重启只结算旧运行，不自动重跑模型或脚本。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    await store.transition(env.who, 'task1', 'running', expected_version=1, at=1001.0)

    async def forbidden(*args, **kwargs):
        """恢复时不能调用执行器。"""
        pytest.fail('不得重放遗留执行')

    registry = TaskRegistry(store, SimpleNamespace(run=forbidden), process_owner='p2', now=lambda: 1002.0)
    try:
        await registry.open(scan=False)
        current = await store.get(env.who, 'task1')
        assert current.status == 'interrupted'
        assert current.error_code == 'process_restarted'
    finally:
        await registry.close()


async def test_shutdown_waits_for_cancel_cleanup(task_env):
    """关闭等待被取消的执行者完成清理，再结算中断。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def run(*args, **kwargs):
        """取消后真实执行finally，不能把发送取消当作退出。"""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    registry = TaskRegistry(store, SimpleNamespace(run=run), process_owner='p1', now=lambda: 1001.0)
    await registry.open(scan=False)
    registry.start(env.who, env.record)
    await asyncio.wait_for(entered.wait(), 5)
    await registry.close()
    assert cleaned.is_set()
    assert (await store.get(env.who, 'task1')).status == 'interrupted'


async def test_write_is_marked_before_approved_execution_and_cancel_is_unknown(task_env):
    """在执行者得到批准前已持久标记，未收到写回执的取消不能称已撤销。"""
    from xuanpu_server.work_tasks.decisions import ToolApprovalDecision

    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who, env.record)
    task = await store.transition(env.who, task.task_id, 'running', expected_version=1, at=1001.0)
    task = await store.transition(env.who, task.task_id, 'waiting_user', expected_version=task.state_version,
        at=1001.0, pending=({'request_id':'approval1','related_call_id':'write1',
                            'detail':{'scope':'tool_use','target':'erp.save'}},))
    entered = asyncio.Event()

    async def writing(who, task, decision=None):
        """模拟发出后没有确定回执的外部写操作。"""
        assert (await store.get(who, task.task_id)).unverified_write
        entered.set()
        await asyncio.Event().wait()

    registry = TaskRegistry(store, SimpleNamespace(run=writing), process_owner='p1', now=lambda:1002.0)
    await registry.open(scan=False)
    try:
        decision = ToolApprovalDecision.model_validate({'kind':'tool_approval','request_id':'approve',
            'state_version':task.state_version,'decisions':{'approval1':{'granted':True}}})
        await registry.decide(env.who, task, decision)
        await asyncio.wait_for(entered.wait(), 5)
        await registry.cancel(env.who, task)
        assert (await store.get(env.who, task.task_id)).status == 'outcome_unknown'
    finally:
        await registry.close()


async def test_resume_reserves_quota_before_consuming_decision(task_env):
    """额度满时决定未被消费；释放后同一请求可且只可恢复一次。"""
    from dataclasses import replace

    from xuanpu_server.errors import ConflictError
    from xuanpu_server.tenancy import QuotaLimits
    from xuanpu_server.work_tasks.decisions import ToolApprovalDecision

    env = task_env
    tenant = await env.control.get_tenant('acme')
    await env.control.update_tenant(replace(tenant, quota=QuotaLimits(max_concurrent_turns=1)))
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who, env.record)
    task = await store.transition(env.who, task.task_id, 'running', expected_version=1, at=1001.0)
    task = await store.transition(env.who, task.task_id, 'waiting_user',
        expected_version=task.state_version, at=1001.0,
        pending=({'request_id':'approval1','detail':{'scope':'shell_exec'}},))
    # 第二位员工有独立的本人来源文件，不借用第一位员工的引用。
    bob = replace(env.who, user_id='usr_b')
    source = await env.workspace.upload(bob, 'bob.txt', b'private minutes', 'text/plain', 'bob-input')
    other = env.record.model_copy(update={'task_id':'task2', 'user_id':bob.user_id, 'actor':bob.actor,
        'request_id':'req2', 'inputs':(env.record.inputs[0].model_copy(update={'item_id':source['id']}),)})
    await store.create_or_replay(bob, other)
    entered, release = asyncio.Event(), asyncio.Event()
    resumed = []

    async def run(who, current, decision=None):
        """第二人占用额度，第一人的恢复应仅在额度释放后发生。"""
        if current.task_id == 'task2':
            entered.set()
            await release.wait()
        else:
            resumed.append(current.task_id)
        return {'status':'waiting_user'}

    registry = TaskRegistry(store, SimpleNamespace(run=run), process_owner='p1', now=lambda:1002.0)
    await registry.open(scan=False)
    try:
        registry.start(bob, other)
        await asyncio.wait_for(entered.wait(), 5)
        decision = ToolApprovalDecision(kind='tool_approval', request_id='approve',
            state_version=task.state_version, decisions={'approval1':{'granted':False}})
        with pytest.raises(ConflictError):
            await registry.decide(env.who, task, decision)
        unchanged = await store.get(env.who, task.task_id)
        assert unchanged.status == 'waiting_user' and not unchanged.decision_fingerprints
        release.set()
        await registry.join()
        await registry.decide(env.who, task, decision)
        await registry.join()
        await registry.decide(env.who, task, decision)
        assert resumed == ['task1']
    finally:
        release.set()
        await registry.close()


async def test_restart_cleans_terminal_with_unverified_resources(task_env):
    """清理未核实的终态仍须恢复资源，完成清理不重跑任务或改变业务结论。"""
    env = task_env
    store = WorkTaskStore(env.workspace)
    await store.create_or_replay(env.who, env.record)
    await store.transition(env.who, 'task1', 'running', expected_version=1, at=1001.0)
    await store.settle_lifecycle('acme', 'usr_a', 'task1', 'outcome_unknown',
        at=1002.0, error_code='container_cleanup_unverified')
    recovered = []

    async def recover(task):
        """按已知任务资源恢复，不调用模型。"""
        recovered.append(task.task_id)

    async def forbidden(*args, **kwargs):
        """终态只能清理资源，不得启动业务。"""
        pytest.fail('不得重放终态任务')

    registry = TaskRegistry(store, SimpleNamespace(run=forbidden), process_owner='p2',
        now=lambda:1003.0, recover=recover)
    try:
        await registry.open(scan=False)
        assert recovered == ['task1']
        current = await store.get(env.who, 'task1')
        assert current.status == 'outcome_unknown'
        assert not current.resources_pending
        await registry.reconcile()
        assert recovered == ['task1']
    finally:
        await registry.close()


async def test_untransferred_runtime_cleanup_failure_keeps_persistent_ownership(task_env, tmp_path):
    """装配器未交付句柄但已创建资源时，真实Runner不能提前清理输入并标记资源已退出。"""
    from xuanpu_server.errors import RuntimeCleanupRequiredError
    from xuanpu_server.work_tasks.inputs import InputSnapshotService
    from xuanpu_server.work_tasks.runner import TaskRunner

    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who,env.record)
    inputs = InputSnapshotService(env.workspace,tmp_path/'snapshots')
    package = SimpleNamespace(record=SimpleNamespace(digest=task.package_digest))
    recovered = []

    async def resolve(*args):
        """包摘要已固定；本测试故障发生在资源装配边界。"""
        return package

    async def remaining_resource():
        """装配器持有尚未退出的资源清理责任。"""
        raise RuntimeError('资源尚未确认退出')

    async def get(*args, **kwargs):
        """资源创建后清理失败，句柄因此尚未移交给Runner。"""
        raise RuntimeCleanupRequiredError('启动结果未知',remaining_resource)

    runner = TaskRunner(store,SimpleNamespace(resolve=resolve),SimpleNamespace(get=get),None,
        inputs,SimpleNamespace(now=lambda:1002.0),None)
    registry = TaskRegistry(store,runner,process_owner='p1',now=lambda:1002.0)
    await registry.open(scan=False)
    registry.start(env.who,task)
    await registry.join()
    current = await store.get(env.who,task.task_id)
    assert current.status == 'outcome_unknown' and current.resources_pending and not current.resources_recovered
    assert inputs.directory(task).exists()
    await registry.close()

    async def recover(current):
        """独占重启才能根据已知任务资源恢复清理。"""
        recovered.append(current.task_id)
        await inputs.cleanup(current)

    fresh = TaskRegistry(store,runner,process_owner='p2',now=lambda:1003.0,recover=recover)
    try:
        await fresh.open(scan=False)
        assert recovered == [task.task_id]
        assert not inputs.directory(task).exists()
        assert not (await store.get(env.who,task.task_id)).resources_pending
    finally:
        await fresh.close()


async def test_shutdown_waits_for_decision_transaction_and_never_launches_after_close(task_env, monkeypatch):
    """批准已预留额度但事务尚未提交时关闭，必须等移交并收回，不能关闭后再启动。"""
    from contextlib import asynccontextmanager

    from xuanpu_server.work_tasks.decisions import ToolApprovalDecision

    env = task_env
    store = WorkTaskStore(env.workspace)
    task, _ = await store.create_or_replay(env.who,env.record)
    task = await store.transition(env.who,task.task_id,'running',expected_version=1,at=1001.0)
    task = await store.transition(env.who,task.task_id,'waiting_user',expected_version=task.state_version,at=1001.0)
    transaction_entered, transaction_release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    late_calls = []
    original = env.workspace.transaction

    @asynccontextmanager
    async def delayed_transaction(who, request_id=None):
        """只控制员工事务边界，不替换其锁、提交或回滚语义。"""
        if request_id == 'work-task:decision':
            transaction_entered.set()
            await transaction_release.wait()
        async with original(who,request_id) as state:
            yield state

    async def run(*args):
        """关闭已返回后不允许任何新执行。"""
        late_calls.append(closed.is_set())
        await asyncio.Event().wait()

    registry = TaskRegistry(store,SimpleNamespace(run=run),process_owner='p1',now=lambda:1002.0)
    await registry.open(scan=False)
    monkeypatch.setattr(env.workspace,'transaction',delayed_transaction)
    decision = ToolApprovalDecision.model_validate({'kind':'tool_approval','request_id':'during-close',
        'state_version':task.state_version,'decisions':{'local':{'granted':False}}})
    pending = asyncio.create_task(registry.decide(env.who,task,decision))
    await asyncio.wait_for(transaction_entered.wait(),5)
    shutdown = asyncio.create_task(registry.close())
    await asyncio.sleep(0)
    assert not shutdown.done()
    transaction_release.set()
    await asyncio.wait_for(pending,5)
    await asyncio.wait_for(shutdown,5)
    closed.set()
    await asyncio.sleep(0)
    assert not any(late_calls) and not registry._active and not registry._executing
