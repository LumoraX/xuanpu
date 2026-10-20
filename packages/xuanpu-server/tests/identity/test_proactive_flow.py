"""主动模型判断、对话确认、原生提醒与完成停止的真实 HTTP 流程。"""
from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from xuanpu_server.controlplane.models import AuditQuery


async def test_proactive_chat_to_native_reminder_and_complete(identity_world, step):
    """真实身份和模型适配经过持久证据、确认、提醒队列及原子取消。"""
    async with identity_world() as ident:
        user=await ident.user()
        headers={**ident.cookie(await ident.login(user)),'X-Requested-By':'xuanpu'}
        p=ident.world.platform
        ident.world.models.script(step(text=json.dumps({'package':None,'entry':None,'message':'报价内容可以继续在这里整理。'})))
        routed=await ident.http.post('/v1/assistant/route',headers=headers,json={'text':'下午把报价发给客户','request_id':'promise'})
        assert routed.status_code==200,routed.text
        followup={'title':'发送报价','intent_type':'commitment','certainty':'explicit',
            'reason':'下午发送报价的承诺还没有确认结果，完成了吗？','evidence_refs':['message:1'],
            'item_id':None,'followup_id':None,'proposed_time':None,'next_review_at':None,'suggested_action':'ask_status'}
        ident.world.models.script(step(text=json.dumps({'followups':[followup]})))
        assert await p.proactive.evaluate_owner('acme',user.user_id) is True
        records=(await ident.http.get('/v1/proactive',headers=headers)).json()['items']
        assert len(records)==1
        current=records[0]
        assert 'message:1' in current['evidence_refs']
        audits=await p.store.query_audit('acme',AuditQuery(kind='proactive_sampled',user_id=user.user_id))
        assert len(audits)==1 and audits[0].data['total_tokens']>0
        due=time.time()+120
        iso=datetime.fromtimestamp(due,UTC).isoformat()
        ident.world.models.script(step(text=json.dumps({'package':'@proactive','entry':'action','message':None,
            'proactive':{'id':current['id'],'version':current['version'],'action':'set_reminder','due_at':iso,'timezone':'UTC'}})))
        proposal=await ident.http.post('/v1/assistant/route',headers=headers,json={'text':'两分钟后提醒我跟进这份报价','request_id':'arrange'})
        assert proposal.status_code==200,proposal.text
        assert proposal.json()['proactive_draft']['id']==current['id']
        assert (await ident.http.get('/v1/reminders',headers=headers)).json()['items']==[]
        body={'request_id':'confirm-active','version':current['version'],'action':'set_reminder','due_at':due,'timezone':'UTC','channels':['inbox']}
        response=await ident.http.post(f"/v1/proactive/{current['id']}/actions",headers=headers,json=body)
        assert response.status_code==200,response.text
        waiting=response.json()
        assert waiting['status']=='waiting' and waiting['reminder_id']
        replay=await ident.http.post(f"/v1/proactive/{current['id']}/actions",headers=headers,json=body)
        assert replay.json()==waiting
        reminders=(await ident.http.get('/v1/reminders',headers=headers)).json()['items']
        assert len(reminders)==1 and reminders[0]['status']=='active'
        progress=await ident.http.post(f"/v1/proactive/{current['id']}/actions",headers=headers,
            json={'request_id':'progress-active','version':waiting['version'],'action':'progress'})
        assert progress.status_code==200,progress.text
        assert progress.json()['status']=='waiting' and 'reminder_id' not in progress.json()
        assert (await ident.http.get('/v1/reminders',headers=headers)).json()['items'][0]['status']=='cancelled'
        done=await ident.http.post(f"/v1/proactive/{current['id']}/actions",headers=headers,
            json={'request_id':'complete-active','version':progress.json()['version'],'action':'complete'})
        assert done.status_code==200,done.text
        assert done.json()['status']=='resolved'
        reminders=(await ident.http.get('/v1/reminders',headers=headers)).json()['items']
        assert reminders[0]['status']=='cancelled'
        stranger=await ident.user(name='别人')
        other={**ident.cookie(await ident.login(stranger)),'X-Requested-By':'xuanpu'}
        assert (await ident.http.get(f"/v1/proactive/{current['id']}",headers=other)).status_code==404
        assert (await ident.http.post(f"/v1/proactive/{current['id']}/actions",headers=ident.cookie(await ident.login(user)),json=body)).status_code==403
