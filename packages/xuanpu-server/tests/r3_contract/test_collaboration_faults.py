"""真实PG子进程提交前后SIGKILL，不借内存重建冒充进程故障验收。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from xuanpu_server.controlplane.postgres import PostgresControlPlane

from .test_approval_decision import decide, prepared, request_approval
from .test_handoff_claim import act, submit


async def stage(http,shared_id,action):
    """通过正式HTTP走到实际故障动作前，不手改持久业务状态。"""
    if action=='accept':
        created=await submit(http,shared_id);assert created.status_code==201,created.text
        return created.json()['id'],'bob',1,{}
    rule,hid,ref=await prepared(http,shared_id)
    requested=await request_approval(http,rule,hid,ref);assert requested.status_code==201,requested.text
    aid=requested.json()['id']
    if action=='approve':
        return aid,'alice',1,{}
    assert (await decide(http['alice'],aid,'approve',request='fault-prior-approve')).status_code==200
    if action=='deliver':
        return hid,'bob',3,{'result_refs':[ref],'approval_id':aid}
    assert (await act(http['bob'],hid,'deliver',3,'fault-prior-deliver',result_refs=[ref],approval_id=aid)).status_code==200
    return hid,'alice',4,{'delivery_version':1}


async def worker(world,config,tmp_path,dsn):
    """进程只读取权限600的独占测试命名空间配置，不接触生产。"""
    namespace=world.platform.store._namespace
    config.update(dsn=dsn,schema=namespace.schema,prefix=namespace.prefix,tenant='acme',
        objects_dir=str(world.objects_dir),marker=str(tmp_path/'fault-marker.json'))
    path=tmp_path/'collaboration-worker.json';path.write_text(json.dumps(config));path.chmod(0o600)
    script=Path(__file__).resolve().parents[4]/'scripts/acceptance/r3_collaboration_worker.py'
    return await asyncio.create_subprocess_exec(sys.executable,str(script),'--config',str(path),
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)


@pytest.mark.parametrize('action,event_kind',[
    ('accept','handoff.accept'),('approve','organizational_approval.approve'),
    ('deliver','handoff.deliver'),('sign_off','handoff.sign_off')])
@pytest.mark.parametrize('phase',['before_commit','after_commit'])
async def test_sigkill_coordination_replay_is_single_and_atomic(handoff_http,shared_id,r3_world,tmp_path,postgres_dsn,action,event_kind,phase):
    """真实退出后重放原请求只有一条决定、一次责任或一条交付/签收及对应通知。"""
    if not isinstance(r3_world.platform.store,PostgresControlPlane):
        pytest.skip('真实进程故障仅对PostgreSQL执行')
    http=handoff_http;identifier,user,version,extra=await stage(http,shared_id,action)
    body={'request_id':'sigkill-original-'+action,'confirmed':True,'expected_version':version,**extra}
    async with r3_world.platform.coordination.transaction('acme') as tx:
        notification_count=len(tx.collaboration.notifications)
    child=await worker(r3_world,{'action':action,'identifier':identifier,'user_id':'usr_'+user,
        'event_kind':event_kind,'phase':phase,'body':body},tmp_path,postgres_dsn)
    try:
        async with asyncio.timeout(20):
            while not (tmp_path/'fault-marker.json').exists():
                if child.returncode is not None:
                    _,stderr=await child.communicate();raise AssertionError('隔离进程提前退出：'+stderr.decode()[-1000:])
                await asyncio.sleep(.05)
        child.kill();await asyncio.wait_for(child.wait(),10);assert child.returncode==-9
        async with r3_world.platform.coordination.transaction('acme') as tx:
            prior=[e for e in tx.state.events.values() if e.kind==event_kind and e.entity_id==identifier]
            assert len(prior)==int(phase=='after_commit')
            if phase=='before_commit':
                assert len(tx.collaboration.notifications)==notification_count
        path=('/v1/approval-requests/' if action=='approve' else '/v1/handoffs/')+identifier+'/'+action.replace('_','-')
        reply=await http[user].post(path,json=body);assert reply.status_code==200,reply.text
        assert (await http[user].post(path,json=body)).json()==reply.json()
        async with r3_world.platform.coordination.transaction('acme') as tx:
            events=[e for e in tx.state.events.values() if e.kind==event_kind and e.entity_id==identifier]
            assert len(events)==1
            notes=[n for n in tx.collaboration.notifications.values() if n.event_id==events[0].id]
            assert len(notes)==1
    finally:
        if child.returncode is None:
            child.kill();await child.wait()
