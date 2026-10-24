"""两名明确员工的到人闭环功能验收，固定成果、组织决定、交付和签收分别核对。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
from r3_shared_matters import call, change, private_config


async def exercise(config: dict[str,Any]) -> dict[str,Any]:
    """只用两名已登录员工会话与预配策略，不自动登录、扩权或推断业务职责。"""
    origin,prefix=config['base_url'],config['request_prefix']
    headers={'Origin':origin,'X-Requested-By':'xuanpu'}
    cookie='xuanpu_session' if config.get('synthetic') else '__Host-xuanpu_session'
    async with httpx.AsyncClient(base_url=origin,cookies={cookie:config['alice_session']},headers=headers,
            timeout=40,trust_env=False) as owner,httpx.AsyncClient(base_url=origin,cookies={cookie:config['bob_session']},
            headers=headers,timeout=40,trust_env=False) as assignee:
        identities=[await call(c,'GET','/v1/me') for c in (owner,assignee)]
        if any(m.get('key_id') is not None or m['tenant_id']!=config['tenant_id'] or not m.get('user_id') for m in identities):
            raise ValueError('需要同一试点企业的两名员工会话，不接受root或API key代替本人')
        if identities[0]['user_id']==identities[1]['user_id']:
            raise ValueError('发起与承办必须是两名不同员工')
        created=await call(owner,'POST','/v1/shared-matters',expected=201,json={'request_id':prefix+':source',
            'confirmed':True,'title':'到人协作功能验收','content':'只核对此次明确交办的材料，不执行外部业务写入。'})
        path='/v1/shared-matters/'+created['id']
        handoff=await call(owner,'POST','/v1/handoffs',expected=201,json=change(prefix,'submit',1,
            shared_id=created['id'],target_kind='user',target_id=identities[1]['user_id'],
            title='到人功能闭环核对',summary='核对并提交本次固定成果，组织批准后独立交付和签收。',
            include_content=True,policy_id=config['policy_id']))
        hp='/v1/handoffs/'+handoff['id']
        accepted=await call(assignee,'POST',hp+'/accept',json=change(prefix,'accept',1))
        if await call(assignee,'POST',hp+'/accept',json=change(prefix,'accept',1))!=accepted:
            raise RuntimeError('接收回执重试不一致')
        await call(assignee,'POST',hp+'/start',json=change(prefix,'start',2))
        visible=await call(assignee,'GET',path)
        data=('固定成果编号：'+prefix+'\n核对结果：完成材料核对，等待发起人独立签收。\n').encode()
        publication=await call(assignee,'POST',hp+'/result-uploads',expected=202,
            params=change(prefix,'result-upload',3,shared_version=visible['version'],name='到人验收成果.txt'),
            content=data,headers={'Content-Type':'text/plain'})
        ready=await call(assignee,'GET',hp+'/result-publications/'+publication['id'])
        if ready['status']!='ready':
            raise RuntimeError('成果尚未ready，不声明成果验收通过')
        material=next(m for m in (await call(assignee,'GET',path+'/materials'))['items'] if m['id']==ready['material_id'])
        revision=material['revisions'][0]
        ref={'material_id':material['id'],'revision':revision['revision'],'sha256':revision['sha256']}
        for actor in (owner,assignee):
            response=await actor.get(path+'/materials/'+material['id']+'/content',params={'revision':ref['revision']})
            if response.status_code!=200 or hashlib.sha256(response.content).hexdigest()!=hashlib.sha256(data).hexdigest():
                raise RuntimeError('本人读取的实际固定成果字节不一致')
        approval=await call(assignee,'POST','/v1/approval-requests',expected=201,json={
            'request_id':prefix+':approval','confirmed':True,'policy_id':config['policy_id'],
            'target_type':'handoff','target_id':handoff['id'],'target_version':3,'result_refs':[ref]})
        ap='/v1/approval-requests/'+approval['id']
        request=await call(owner,'GET',ap)
        if request['steps']!=[{'index':0,'user_id':identities[0]['user_id']}] or 'approve' not in request['allowed_actions']:
            raise RuntimeError('策略未固定到发起人的单步核对，不自动替换审批人')
        approved=await call(owner,'POST',ap+'/approve',json=change(prefix,'approve',1,reason='已核对本次固定成果。'))
        if await call(owner,'POST',ap+'/approve',json=change(prefix,'approve',1,reason='已核对本次固定成果。'))!=approved:
            raise RuntimeError('组织决定重试不一致')
        if (await call(owner,'GET',hp))['status']!='in_progress':
            raise RuntimeError('组织批准错误改变交付或签收状态')
        delivered=await call(assignee,'POST',hp+'/deliver',json=change(prefix,'deliver',3,result_refs=[ref],approval_id=approval['id']))
        if delivered['status']!='delivered' or delivered['delivery_version']!=1:
            raise RuntimeError('交付未固定预期版本')
        signed=await call(owner,'POST',hp+'/sign-off',json=change(prefix,'sign',4,delivery_version=1))
        if await call(owner,'POST',hp+'/sign-off',json=change(prefix,'sign',4,delivery_version=1))!=signed:
            raise RuntimeError('签收重试不一致')
        final=await call(owner,'GET',hp)
        if final['status']!='signed_off' or len(final['deliveries'])!=1 or sum(e['action']=='sign_off' for e in final['history'])!=1:
            raise RuntimeError('独立业务检查未发现唯一固定交付和签收')
        return await cleanup(owner,assignee,path,final,approval['id'],ref,prefix,identities,config)


async def cleanup(owner: httpx.AsyncClient,assignee: httpx.AsyncClient,path: str,final: dict[str,Any],
                  approval: str,ref: dict[str,Any],prefix: str,identities: list[Any],config: dict[str,Any]) -> dict[str,Any]:
    """收回本合成/试点资料用途并归档，不删除业务决定或已发生的签收历史。"""
    members=(await call(owner,'GET',path+'/members'))['items']
    for index,member in enumerate(members):
        if member['role']=='owner' or member['status']!='active':
            continue
        current=await call(owner,'GET',path)
        await call(owner,'POST',path+'/members/'+member['id']+'/revoke',json=change(prefix,'cleanup-'+str(index),current['version']))
    denied=await assignee.get(path)
    if denied.status_code!=404:
        raise RuntimeError('测试用途授权未收回')
    current=await call(owner,'GET',path)
    await call(owner,'PATCH',path,json=change(prefix,'archive',current['version'],status='archived'))
    return {'status':'passed','synthetic':bool(config.get('synthetic')),'actors':[m['user_id'] for m in identities],
        'handoff_id':final['id'],'approval_id':approval,'shared_id':current['id'],'result_sha256':ref['sha256'],
        'business_check':{'fixed_delivery_count':1,'signoff_count':1,'approval_separate_from_signoff':True},
        'real_model':'not_checked','production_release':'not_proven','runner_external_business_write_requests':0,
        'cleanup':{'purpose_grants':'revoked','matter':'archived','business_history':'retained'}}


def main() -> None:
    """显式执行才产生测试交接和站内动态，报告不含会话、正文或凭据。"""
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    if not args.execute:
        parser.error('需要明确--execute；将创建、签收并归档本次验收工作')
    report=asyncio.run(exercise(private_config(args.config)))
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2));args.output.chmod(0o600)
    print(json.dumps({'status':report['status'],'synthetic':report['synthetic']}))


if __name__=='__main__':
    main()
