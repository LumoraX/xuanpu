"""R2默认兼容且固定员工委托引用不能复活。"""
from __future__ import annotations

import pytest
from xuanpu_server.errors import PermissionDeniedError
from xuanpu_server.tenancy import Principal

from ..conftest import docker_test_user


async def test_cold_recovery_does_not_adopt_new_consent(r3_world,r3_http,r3_connected):
    """撤销再同意也不授权原任务，旧引用失效为明确阻塞。"""
    from xuanpu_server.connections.models import ConnectionRef
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.connections.task_bindings import check_refs
    cid,consent=r3_connected
    service=r3_world.platform.connections
    who=Principal(role='tenant_member',tenant_id='acme',key_id=None,user_id='usr_alice')
    ref=ConnectionRef(installation_id=cid,installation_version=2,
                      consent_id=consent['consent_id'],consent_epoch=1)
    await check_refs(service,who,(ref,))
    await r3_http['alice'].post(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}/revoke",json={
        'request_id':'revoke-old','expected_version':1})
    import time
    await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
        'request_id':'new-consent','operations':['self_profile.read'],
        'purpose':'新授权','expires_at':time.time()+3600})
    fresh=ConnectionService(service.store,service.control,service.identity,config=service.config)
    with pytest.raises(PermissionDeniedError,match='connection_reauthorization_required'):
        await check_refs(fresh,who,(ref,))


async def test_real_r2_task_blocks_and_explicit_resume_preserves_budget(r3_world,r3_http,r3_connected,r3_package):
    """真实受理授权、派发及恢复路径，无模型口述或直接伪造状态。"""
    import time

    from xuanpu_server.work_tasks.config import DurableExecutionConfig
    from xuanpu_server.work_tasks.models import CapabilityTaskInput
    platform=r3_world.platform
    cid,consent=r3_connected
    result=await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/package-bindings',json={
        'request_id':'r2-binding','package_digest':r3_package.digest.removeprefix('sha256:'),
        'connector_name':'wecom','operation_id':'self_profile','operation':'self_profile.read',
        'endpoint':'/cgi-bin/user/get','server_bound_parameters':['userid','access_token']})
    assert result.status_code==201,result.text
    config=DurableExecutionConfig(enabled=True,verified_text_bounds=True)
    tasks=platform.work_tasks
    tasks.durable=config;tasks.store.durable_config=config;platform.task_registry.durable.config=config
    platform.task_registry.durable.execution.verified_text_bounds=True
    executor=tasks.executor;tasks.executor=None
    who=Principal(role='tenant_member',tenant_id='acme',key_id=None,user_id='usr_alice')
    try:
        task=await tasks.accept_capability(who,CapabilityTaskInput.model_validate({
            'request_id':'connection-task','text':'核对本人基础资料',
            'capability':{'package':'wecom-readonly','entry':'read'}}))
        assert task.connection_refs[0].consent_id==consent['consent_id']
        execution=platform.task_registry.durable.execution
        grant_before=await execution.grant(who,task.grant_id)
        await r3_http['alice'].post(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}/revoke",json={
            'request_id':'r2-revoke','expected_version':1})
        await platform.task_registry.durable.reconcile(task,restart=True)
        dispatch=await execution.get(who,task.task_id)
        assert dispatch.state=='blocked'
        assert dispatch.block_reason=='connection_reauthorization_required'
        assert dispatch.attempt_no==0
        response=await r3_http['alice'].post(f'/v1/work-tasks/{task.task_id}/decisions',json={
            'kind':'tool_approval','request_id':'stale-tool','state_version':1,
            'decisions':{'old':{'granted':True}}})
        assert response.status_code==409,response.text
        response=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
            'request_id':'r2-new','operations':['self_profile.read'],
            'purpose':'本人重新确认','expires_at':time.time()+3600})
        assert response.status_code==201,response.text
        # 新同意本身不恢复旧task；必须再明确继续原任务。
        await platform.task_registry.durable.reconcile(task,restart=True)
        assert (await execution.get(who,task.task_id)).state=='blocked'
        current=await tasks.store.get(who,task.task_id)
        response=await r3_http['alice'].post(f'/v1/work-tasks/{task.task_id}/connection-authorization',json={
            'request_id':'r2-resume','expected_version':current.state_version})
        assert response.status_code==200,response.text
        grant_after=await execution.grant(who,task.grant_id)
        assert grant_before.model_dump(exclude={'connection_refs'})==grant_after.model_dump(exclude={'connection_refs'})
        assert grant_after.connection_refs!=grant_before.connection_refs
        assert (await execution.get(who,task.task_id)).state=='ready'
    finally:
        tasks.executor=executor


async def test_inflight_task_revoke_blocks_before_terminal_result(r3_world,r3_http,r3_connected,r3_package,step):
    """真正内核工具在途撤权，任务保留可显式续接状态和已耗预算。"""
    import asyncio
    import time

    import httpx
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.work_tasks.config import DurableExecutionConfig
    from xuanpu_server.work_tasks.models import TERMINAL, CapabilityTaskInput
    platform=r3_world.platform
    cid,consent=r3_connected
    result=await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/package-bindings',json={
        'request_id':'inflight-binding','package_digest':r3_package.digest.removeprefix('sha256:'),
        'connector_name':'wecom','operation_id':'self_profile','operation':'self_profile.read',
        'endpoint':'/cgi-bin/user/get','server_bound_parameters':['userid','access_token']})
    assert result.status_code==201,result.text
    from pathlib import Path

    from xuanpu_server.runtime.sandbox import DockerSandboxProvider
    platform.runtimes._sandboxes=DockerSandboxProvider(user=docker_test_user(), image="python:3.12-slim",memory_mb=512,
        docker_env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':str(Path.home())},
        labels={'xuanpu.test':'r3-active-revoke'})
    started,release=asyncio.Event(),asyncio.Event()
    async def handler(request):
        if request.url.path.endswith('gettoken'):
            return httpx.Response(200,json={'errcode':0,'access_token':'token','expires_in':7200})
        started.set();await release.wait()
        return httpx.Response(200,json={'errcode':0,'userid':'alice','name':'late-private-name'})
    provider=WeComClient(transport=httpx.MockTransport(handler))
    platform.connection_tokens.provider=provider;platform.connection_broker.provider=provider
    tasks=platform.work_tasks
    config=DurableExecutionConfig(enabled=True,verified_text_bounds=True)
    tasks.durable=config;tasks.store.durable_config=config;platform.task_registry.durable.config=config
    platform.task_registry.durable.execution.verified_text_bounds=True
    executor=tasks.executor;tasks.executor=None
    who=Principal(role='tenant_member',tenant_id='acme',key_id=None,user_id='usr_alice')
    pending=None
    try:
        task=await tasks.accept_capability(who,CapabilityTaskInput.model_validate({
            'request_id':'inflight-task','text':'核对本人资料','capability':{'package':'wecom-readonly','entry':'read'}}))
        r3_world.models.script(step(calls=[('c1','api__wecom__self_profile',{})]),step(text='本次读取停止'))
        pending=asyncio.create_task(platform.task_registry.durable.execute(who,task,None))
        await asyncio.wait_for(started.wait(),10)
        response=await r3_http['alice'].post(f"/v1/me/connections/{cid}/consents/{consent['consent_id']}/revoke",json={
            'request_id':'inflight-task-revoke','expected_version':1})
        assert response.status_code==200,response.text
        release.set();await asyncio.wait_for(pending,10)
        current=await tasks.store.get(who,task.task_id)
        execution=platform.task_registry.durable.execution
        dispatch=await execution.get(who,task.task_id)
        assert current.status not in TERMINAL,current
        assert not current.resources_pending
        assert dispatch.state=='blocked'
        assert dispatch.block_reason=='connection_reauthorization_required'
        assert dispatch.attempt_no==1
        before=await execution.grant(who,task.grant_id)
        response=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
            'request_id':'inflight-new','operations':['self_profile.read'],'purpose':'重新确认',
            'expires_at':time.time()+3600})
        assert response.status_code==201,response.text
        response=await r3_http['alice'].post(f'/v1/work-tasks/{task.task_id}/connection-authorization',json={
            'request_id':'inflight-resume','expected_version':current.state_version})
        assert response.status_code==200,response.text
        after=await execution.grant(who,task.grant_id)
        assert before.model_dump(exclude={'connection_refs'})==after.model_dump(exclude={'connection_refs'})
        assert (await execution.get(who,task.task_id)).state=='ready'
        assert 'late-private-name' not in str(r3_world.models.requests)
    finally:
        tasks.executor=executor
        release.set()
        if pending is not None and not pending.done():
            pending.cancel();await asyncio.gather(pending,return_exceptions=True)
        await provider.close()
