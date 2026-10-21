"""权威核实只附加证据，不能回退未知终态或自动重发。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.runtime.effect_reconciliation import EffectReconciliation, ReconciliationProof
from xuanpu_server.runtime.effect_store import EffectStore
from xuanpu_server.work_tasks.execution_context import ExecutionBinding

from .test_execution_store import prepared


async def test_authoritative_not_applied_keeps_original_unknown_terminal(task_env):
    """没有适配器则阻塞；固定适配器查询后保留原任务终态。"""
    tasks, execution, task = await prepared(task_env)
    lease = await execution.claim(task_env.who,task.task_id,owner='p',now=1000.)
    effect = await EffectStore(ExecutionBinding(lease,execution,lambda:1000.)).begin(
        call_id='c1',tool='api__controlled__write',arguments_digest='c'*64)
    await tasks.settle_lifecycle('acme','usr_a',task.task_id,'outcome_unknown',at=1001.,error_code='lost_reply')
    current = await tasks.get(task_env.who,task.task_id)
    service = EffectReconciliation(task_env.workspace,now=lambda:1002.)
    with pytest.raises(ConflictError,match='权威'):
        await service.verify(task_env.who,task.task_id,effect.logical_action_id,expected_version=current.state_version)
    class Adapter:
        """受控下游账本返回确定未应用，不以HTTP错误推断。"""
        async def verify(self, receipt):
            return ReconciliationProof(state='confirmed_not_applied',authority='controlled-journal-v1',evidence_digest='d'*64)
    service.adapters['api__controlled__write'] = Adapter()
    proof = await service.verify(task_env.who,task.task_id,effect.logical_action_id,expected_version=current.state_version)
    assert proof.state == 'confirmed_not_applied'
    assert (await tasks.get(task_env.who,task.task_id)).status == 'outcome_unknown'
    assert await execution.claim(task_env.who,task.task_id,owner='new',now=1002.) is None


async def test_reconciliation_request_replay_does_not_repeat_authority_query(task_env):
    """原请求在提交后重放，不受旧expected_version影响，不再次访问权威端。"""
    import inspect

    from xuanpu_server.work_tasks.service import WorkTaskService
    tasks,execution,task=await prepared(task_env)
    lease=await execution.claim(task_env.who,task.task_id,owner='p',now=1000.)
    effect=await EffectStore(ExecutionBinding(lease,execution,lambda:1000.)).begin(
        call_id='c1',tool='api__controlled__write',arguments_digest='c'*64)
    await tasks.settle_lifecycle('acme','usr_a',task.task_id,'outcome_unknown',at=1001.,error_code='lost_reply')
    current=await tasks.get(task_env.who,task.task_id)
    service=WorkTaskService(task_env.workspace,None,process_owner='p',now=lambda:1002.)
    assert 'request_id' in inspect.signature(service.reconciliation.verify).parameters,'权威查询尚无持久请求回执'
    calls=[]
    class Adapter:
        """真实权威适配边界，记录查询次数而不写下游。"""
        async def verify(self, receipt):
            calls.append(receipt.logical_action_id)
            return ReconciliationProof(state='confirmed_success',authority='controlled',evidence_digest='d'*64)
    service.reconciliation.adapters['api__controlled__write']=Adapter()
    args={'expected_version':current.state_version,'request_id':'fixed-reconcile'}
    first=await service.reconciliation.verify(task_env.who,task.task_id,effect.logical_action_id,**args)
    again=await service.reconciliation.verify(task_env.who,task.task_id,effect.logical_action_id,**args)
    assert first==again and len(calls)==1
    assert (await tasks.get(task_env.who,task.task_id)).status=='outcome_unknown'
