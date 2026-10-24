"""不同员工并发认领及通知失败不留下半份责任、资料授权或回执。"""
from __future__ import annotations

import asyncio

import pytest

from .test_handoff_claim import act, submit


async def test_two_real_unit_operators_have_exactly_one_assignee(handoff_http,shared_id,organization_ids,other_employee):
    """两个当前合格的不同员工竞争，一人成功，另一人不能继承其资料包。"""
    http=handoff_http;admin=http['tenant_admin'];unit=organization_ids['a']
    for user in ('usr_bob','usr_carol'):
        assert (await admin.post('/v1/admin/organization/memberships',json={
            'request_id':'claim-member-'+user,'user_id':user,'unit_id':unit})).status_code==201
        assert (await admin.post('/v1/admin/organization/roles',json={
            'request_id':'claim-role-'+user,'user_id':user,'scope_id':unit,'role':'unit_operator'})).status_code==201
    created=await submit(http,shared_id,target_kind='unit',target_id=unit,include_content=True)
    assert created.status_code==201,created.text
    hid=created.json()['id']
    clients=[http['bob'],other_employee]
    replies=await asyncio.gather(*(act(c,hid,'accept',1,'different-claim-'+str(i)) for i,c in enumerate(clients)))
    assert sorted(r.status_code for r in replies)==[200,409]
    loser=clients[next(i for i,r in enumerate(replies) if r.status_code==409)]
    assert (await loser.get('/v1/shared-matters/'+shared_id)).status_code==404
    current=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert len([e for e in current['history'] if e['action']=='accept'])==1


async def test_accept_notification_failure_rolls_back_grant_and_receipt(handoff_http,shared_id,monkeypatch):
    """注入事务末端错误后同操作号可重试，不留下已接受但无动态的孤单。"""
    from xuanpu_server.handoffs import events
    http=handoff_http
    created=await submit(http,shared_id);hid=created.json()['id']
    original=events.notify
    def fail(*args,**kwargs):
        """模拟通知仓储写入失败。"""
        raise RuntimeError('injected notification storage failure')
    monkeypatch.setattr(events,'notify',fail)
    with pytest.raises(RuntimeError,match='injected'):
        await act(http['bob'],hid,'accept',1,'atomic-accept')
    monkeypatch.setattr(events,'notify',original)
    assert (await http['bob'].get('/v1/shared-matters/'+shared_id)).status_code==404
    current=(await http['alice'].get('/v1/handoffs/'+hid)).json()
    assert current['status']=='submitted' and current['version']==1
    assert (await act(http['bob'],hid,'accept',1,'atomic-accept')).status_code==200
