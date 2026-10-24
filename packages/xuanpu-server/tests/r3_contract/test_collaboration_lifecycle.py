"""既有治理周期内扫描责任与期限，重启和并发不重复提醒或自动决定。"""
from __future__ import annotations

import asyncio
import time

from .test_handoff_claim import act, submit


async def test_deadline_is_once_across_parallel_scans_and_new_scanner(handoff_http,shared_id,r3_world,monkeypatch):
    """期限只追加通用站内提示，业务仍处于submitted。"""
    from xuanpu_server.collaboration.lifecycle import CollaborationLifecycle
    http=handoff_http;platform=r3_world.platform
    due=time.time()+20
    created=await submit(http,shared_id,due_at=due);assert created.status_code==201
    monkeypatch.setattr(platform.handoffs,'now',lambda:due+1)
    scanners=[CollaborationLifecycle(platform.approvals) for _ in range(2)]
    await asyncio.gather(*(s.recover('acme') for s in scanners))
    await CollaborationLifecycle(platform.approvals).recover('acme')
    async with platform.coordination.transaction('acme') as tx:
        events=[e for e in tx.state.events.values() if e.kind=='handoff.deadline']
        assert len(events)==1 and events[0].actor=='system:collaboration'
        assert tx.collaboration.handoffs[created.json()['id']].status=='submitted'
        notes=[n for n in tx.collaboration.notifications.values() if n.event_id==events[0].id]
        assert len(notes)==2


async def test_governance_scan_blocks_and_revokes_old_purpose_after_api_block(handoff_http,shared_id,r3_world):
    """即使原生查询先发现失效，后台仍收尾旧grant，系统审计不冒充员工。"""
    http=handoff_http;platform=r3_world.platform
    created=await submit(http,shared_id,include_content=True);hid=created.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'scan-accept')).status_code==200
    await platform.identity.store.set_user_status('acme','usr_bob','disabled',reason='admin',at=100)
    blocked=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert blocked['blocked_reason'] is not None
    await platform.collaboration_lifecycle.recover('acme')
    async with platform.coordination.transaction('acme') as tx:
        grants=[g for g in tx.shared.grants.values() if g.handoff_id==hid]
        assert grants and all(g.status=='revoked' for g in grants)
        count=len(tx.collaboration.events)
    await platform.collaboration_lifecycle.recover('acme')
    async with platform.coordination.transaction('acme') as tx:
        assert len(tx.collaboration.events)==count


async def test_governance_auto_block_has_system_actor_without_fake_employee(handoff_http,shared_id,r3_world):
    """真实人员缺资格只阻塞，不生成部门用户或伪造原发起人动作。"""
    http=handoff_http;platform=r3_world.platform
    created=await submit(http,shared_id);hid=created.json()['id']
    await platform.identity.store.set_user_status('acme','usr_bob','disabled',reason='admin',at=100)
    await platform.collaboration_lifecycle.recover('acme')
    async with platform.coordination.transaction('acme') as tx:
        event=next(e for e in tx.collaboration.events.values() if e.handoff_id==hid and e.action=='blocked')
        assert event.actor_id is None
        source=next(e for e in tx.state.events.values() if e.kind=='handoff.blocked')
        assert source.actor=='system:collaboration'
        assert tx.collaboration.handoffs[hid].assignee_id is None


async def test_confirmed_cancel_closes_pending_approval_in_same_transaction(handoff_http,shared_id,r3_world):
    """取消不依赖用户再打开审批卡收尾，也不留下永久pending占用容量。"""
    from .test_approval_decision import prepared, request_approval
    http=handoff_http;platform=r3_world.platform
    rule,hid,ref=await prepared(http,shared_id)
    created=await request_approval(http,rule,hid,ref);assert created.status_code==201,created.text
    aid=created.json()['id']
    assert (await act(http['alice'],hid,'request-cancel',3,'cancel-pending-request',reason='终止本次工作')).status_code==200
    assert (await act(http['bob'],hid,'confirm-cancel',4,'cancel-pending-confirm')).status_code==200
    async with platform.coordination.transaction('acme') as tx:
        assert tx.collaboration.approvals[aid].status=='superseded'
        assert not any(a.status=='pending' for a in tx.collaboration.approvals.values())
    await platform.collaboration_lifecycle.recover('acme')
    assert (await http['bob'].get('/v1/approval-requests/'+aid)).json()['status']=='superseded'
