"""真实发布入口、实际只读工具回执和无附件成功规则。"""
from __future__ import annotations

import asyncio

import pytest
from xuanpu_server.identity.models import UserRecord
from xuanpu_server.tenancy import Principal
from xuanpu_server.work_tasks.config import DurableExecutionConfig
from xuanpu_server.work_tasks.models import CapabilityTaskInput


@pytest.fixture
async def capability_world(identity_stores, world_opener, office_image):
    """真实双后端、公开内核和已发布的合成能力。"""
    from pathlib import Path

    from xuanpu_server.runtime.sandbox import DockerSandboxProvider

    control, identity = identity_stores
    world = await world_opener(store=control, identity_store=identity, sandboxes=DockerSandboxProvider(image=office_image, memory_mb=512,
            docker_env={'PATH':'/usr/local/bin:/usr/bin:/bin', 'HOME':str(Path.home())},
            labels={'xuanpu.test':'r2-capability'}),
        durable_execution=DurableExecutionConfig(enabled=True, verified_text_bounds=True))
    await world.publish()
    await identity.create_user(UserRecord(tenant_id='acme', user_id='usr_cap', display_name='员工', created_at=1.))
    world.employee = Principal(role='tenant_member', tenant_id='acme', user_id='usr_cap', key_id=None)
    try:
        yield world
    finally:
        await world.platform.close()


@pytest.mark.parametrize('with_tool', [True, False])
async def test_capability_needs_real_read_receipt_not_model_claim(capability_world, step, with_tool):
    """实际读取成功无需伪造文件；只有模型口头总结不能算业务完成。"""
    world = capability_world
    if with_tool:
        world.models.script(step(calls=[('read', 'mcp__crm__search_customer', {'query':'C-R2'})]), step(text='已读取客户'))
    else:
        world.models.script(step(text='我声称业务完成'))
    task = await world.platform.work_tasks.accept_capability(world.employee,
        CapabilityTaskInput.model_validate({'request_id':'cap', 'text':'查询客户',
            'capability':{'package':'support-desk','entry':'triage'}}))
    await asyncio.wait_for(world.platform.task_registry.join(), 15)
    current = await world.platform.work_tasks.store.get(world.employee, task.task_id)
    assert current.status == ('succeeded' if with_tool else 'failed'), current
    assert not current.artifacts
    if with_tool:
        assert current.result_receipt['tool_receipts'][0]['call_id'] == 'read'
        public = await world.platform.work_tasks.public_record(world.employee, current)
        assert 'epoch' not in public['result_receipt']['tool_receipts'][0]
        assert not {'process_owner', 'grant_id', 'staging_objects', 'resume_evidence'} & public.keys()
        assert len(world.calls()) == 1
        usage = await world.platform.store.get_usage('acme', next(iter(
            (await world.platform.identity.store.list_user_usage('acme', since='2000', until='2099')))).day)
        assert usage.total_tokens == 240


async def test_recomputed_turn_cannot_borrow_previous_read_receipt(capability_world, step):
    """首轮真实读取后停机，下一轮只有模型文字不能复用旧回执成功。"""
    from xuanpu_server.platform import Platform

    world = capability_world
    hold = asyncio.Event()
    world.models.script(step(calls=[('read-old','mcp__crm__search_customer',{'name':'合成客户'})]),
                        step(text='旧轮等待',wait=hold))
    task = await world.platform.work_tasks.accept_capability(world.employee,
        CapabilityTaskInput.model_validate({'request_id':'read-before-recompute','text':'读取客户',
            'capability':{'package':'support-desk','entry':'triage'}}))
    async with asyncio.timeout(15):
        while len(world.models.requests) < 2:
            await asyncio.sleep(.02)
    old = world.platform
    await old.close()
    first = await old.work_tasks.store.get(world.employee,task.task_id)
    assert first.result_receipt.get('read_receipts') and first.status == 'running'
    world.models.script(step(text='本轮只输出模型说明，并没有调用查询工具'))
    world.platform = Platform(store=old.store, identity_store=old.identity.store,
        identity_settings=old.identity.settings,vault=old.vault,models=world.models,
        sandboxes=old.runtimes._sandboxes,data_dir=world.data_dir,objects=world.objects,
        root_key_hash='0'*64,instance_id='read-recompute',
        durable_execution=DurableExecutionConfig(enabled=True,verified_text_bounds=True))
    await world.platform.start()
    await world.platform.task_registry.reconcile()
    await asyncio.wait_for(world.platform.task_registry.join(),15)
    current = await world.platform.work_tasks.store.get(world.employee,task.task_id)
    assert current.attempt_no == 2 and current.status == 'failed', current
    assert len(world.calls()) == 1


async def test_ordinary_turn_capacity_does_not_spend_background_attempt(capability_world,step,read_turn):
    """已知普通对话占满额度时，后台只持久排队，不提前消费次数。"""
    from dataclasses import replace

    from xuanpu_server.tenancy import QuotaLimits

    world = capability_world
    tenant = await world.platform.store.get_tenant('acme')
    await world.platform.store.update_tenant(replace(tenant,quota=QuotaLimits(max_concurrent_turns=1)))
    hold = asyncio.Event()
    world.models.script(step(text='普通对话正在进行',wait=hold))
    session = await world.platform.sessions.create(world.employee,package='support-desk')
    turn = await world.platform.sessions.send_message(world.employee,session.session_id,'继续普通对话')
    async with asyncio.timeout(10):
        while len(world.models.requests) < 1:
            await asyncio.sleep(.01)
    world.models.script(step(calls=[('read-task','mcp__crm__search_customer',{'name':'合成'})]),step(text='查询完成'))
    task = await world.platform.work_tasks.accept_capability(world.employee,
        CapabilityTaskInput.model_validate({'request_id':'queued-by-ordinary','text':'查询客户',
            'capability':{'package':'support-desk','entry':'triage'}}))
    await world.platform.task_registry.join()
    current = await world.platform.work_tasks.store.get(world.employee,task.task_id)
    assert current.status == 'accepted' and current.attempt_no == 0
    hold.set()
    await read_turn(turn)
    await world.platform.task_registry.reconcile()
    await asyncio.wait_for(world.platform.task_registry.join(),15)
    current = await world.platform.work_tasks.store.get(world.employee,task.task_id)
    assert current.status == 'succeeded' and current.attempt_no == 1


def test_capability_summary_consumes_public_kernel_text_event():
    """真实内核发assistant_text/delta，不能误用网关text_delta协议。"""
    from xuanpu_server.work_tasks.capability_runner import CapabilityEvidence
    evidence=CapabilityEvidence()
    evidence.observe('assistant_text',{'delta':'客户C-R2已验证'},'root-submission')
    assert evidence.texts==['客户C-R2已验证']
