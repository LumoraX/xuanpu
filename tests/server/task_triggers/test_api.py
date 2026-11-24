"""执行安排HTTP复用员工权限、CSRF、严格参数与本人分页。"""
from __future__ import annotations

from ..work_tasks.conftest import task_api  # noqa: F401


async def test_definition_http_isolation_and_replay(task_api):  # noqa: F811
    """公开响应不包含内部grant/owner；管理员也不能读取其他员工内容。"""
    api=task_api
    service=api.world.platform.task_definitions
    service.config=service.config.model_copy(update={'enabled':True})
    service.tasks.durable=service.tasks.durable.model_copy(update={'enabled':True})
    service.now=lambda:1792587600.
    a,b=api.users['usr_a'],api.users['usr_b']
    source=(await api.client.post('/v1/workspace/files',params={'name':'PRIVATE.txt','request_id':'api-input'},
        content=b'PRIVATE minutes',headers={**a,'Content-Type':'text/plain'})).json()
    body={'request_id':'api-create','confirmed':True,'title':'PRIVATE request',
        'template':{'kind':'office','text':'PRIVATE minutes','input_files':[{'item_id':source['id'],'revision':1}]},
        'trigger':{'kind':'time','repeat':'once','at':'2026-10-21T21:01:00+08:00'},
        'valid_until':'2026-10-22T21:01:00+08:00'}
    assert (await api.client.post('/v1/task-definitions',json=body,headers={'Cookie':a['Cookie']})).status_code==403
    assert (await api.client.post('/v1/task-definitions',json={**body,'actor':'fake'},headers=a)).status_code==400
    created=await api.client.post('/v1/task-definitions',json=body,headers=a)
    assert created.status_code==202,created.text
    assert not {'prepared','grant_id','hourly_fires','tenant_id','user_id'} & created.json().keys()
    identifier=created.json()['definition_id']
    assert (await api.client.post('/v1/task-definitions',json=body,headers=a)).json()==created.json()
    assert (await api.client.post('/v1/task-definitions',json={**body,'title':'changed'},headers=a)).status_code==409
    for suffix in ('','/occurrences'):
        response=await api.client.get('/v1/task-definitions/'+identifier+suffix,headers=b)
        assert response.status_code==404 and 'PRIVATE' not in response.text
    assert (await api.client.get('/v1/task-definitions',headers=b)).json()=={'items':[]}
    assert (await api.client.post('/v1/task-definitions/'+identifier+'/cancel',
        json={'request_id':'foreign-cancel','expected_version':1},headers=b)).status_code==404
