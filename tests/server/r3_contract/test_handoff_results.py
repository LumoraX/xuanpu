"""承办人仅能发布本次交接的新成果，不取得原事项editor权限。"""
from __future__ import annotations

from .test_handoff_claim import act, submit


async def test_assignee_publishes_only_new_result_and_owner_can_sign(handoff_http,shared_id):
    """无部门、无模型、无全事项编辑权仍可完成固定结果交付与签收。"""
    http=handoff_http
    created=await submit(http,shared_id,include_content=True);hid=created.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'result-accept')).status_code==200
    assert (await act(http['bob'],hid,'start',2,'result-start')).status_code==200
    source=(await http['bob'].get('/v1/shared-matters/'+shared_id)).json()
    query={'request_id':'new-result','confirmed':'true','expected_version':3,'shared_version':source['version'],'name':'承办成果.txt'}
    uploaded=await http['bob'].post('/v1/handoffs/'+hid+'/result-uploads',params=query,content=b'fixed handoff result')
    assert uploaded.status_code==202,uploaded.text
    replay=await http['bob'].post('/v1/handoffs/'+hid+'/result-uploads',params=query,content=b'fixed handoff result')
    assert replay.json()==uploaded.json(),replay.text
    publication=await http['bob'].get('/v1/handoffs/'+hid+'/result-publications/'+uploaded.json()['id'])
    assert publication.status_code==200 and publication.json()['status']=='ready',publication.text
    material=publication.json()['material_id']
    listings=(await http['bob'].get('/v1/shared-matters/'+shared_id+'/materials')).json()['items']
    revision=next(m for m in listings if m['id']==material)['revisions'][0]
    ref={'material_id':material,'revision':revision['revision'],'sha256':revision['sha256']}
    assert (await act(http['bob'],hid,'deliver',3,'deliver-own-result',result_refs=[ref])).status_code==200
    assert (await act(http['alice'],hid,'sign-off',4,'sign-own-result',delivery_version=1)).status_code==200
    assert (await http['bob'].patch('/v1/shared-matters/'+shared_id,json={
        'request_id':'result-not-editor','confirmed':True,'expected_version':source['version'],'content':'越权'})).status_code==404
    assert (await http['bob'].post('/v1/shared-matters/'+shared_id+'/material-uploads',params={
        'request_id':'ordinary-not-editor','confirmed':'true','expected_version':source['version'],'name':'越权.txt'},content=b'x')).status_code==404


async def test_result_upload_checks_handoff_and_source_versions(handoff_http,shared_id):
    """旧交接版本、错误共享版本或发起人冒充承办人均不能生成半公开成果。"""
    http=handoff_http
    created=await submit(http,shared_id);hid=created.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'version-accept')).status_code==200
    assert (await act(http['bob'],hid,'start',2,'version-start')).status_code==200
    source=(await http['alice'].get('/v1/shared-matters/'+shared_id)).json()
    query={'request_id':'result-version','confirmed':'true','expected_version':2,'shared_version':source['version'],'name':'成果.txt'}
    assert (await http['bob'].post('/v1/handoffs/'+hid+'/result-uploads',params=query,content=b'x')).status_code==409
    assert (await http['bob'].post('/v1/handoffs/'+hid+'/result-uploads',params={**query,'expected_version':3,'shared_version':1},content=b'x')).status_code==409
    assert (await http['alice'].post('/v1/handoffs/'+hid+'/result-uploads',params={**query,'expected_version':3},content=b'x')).status_code in (403,404)
    assert (await http['alice'].get('/v1/shared-matters/'+shared_id+'/materials')).json()['total']==0


async def test_result_copy_source_transfer_fails_before_ready(handoff_http,shared_id,r3_world,monkeypatch):
    """真实对象复制后源owner变化，最终不得公开成果，副本清理不靠进程内标志。"""
    http=handoff_http
    created=await submit(http,shared_id);hid=created.json()['id']
    assert (await act(http['bob'],hid,'accept',1,'copy-accept')).status_code==200
    assert (await act(http['bob'],hid,'start',2,'copy-start')).status_code==200
    path='/v1/shared-matters/'+shared_id
    source=(await http['bob'].get(path)).json()
    objects=r3_world.platform.objects;put=objects.put;copied=[]
    async def transfer_after_copy(key,data,**kwargs):
        """对象I/O不持有数据库事务，可在真实来源写入后再返回复制。"""
        await put(key,data,**kwargs)
        if '/shared/' in key:
            copied.append(key)
            current=(await http['alice'].get(path)).json()
            moved=await http['alice'].post(path+'/transfer-owner',json={'request_id':'copy-owner-transfer',
                'confirmed':True,'expected_version':current['version'],'user_id':'usr_tenant_admin'})
            assert moved.status_code==200,moved.text
    monkeypatch.setattr(objects,'put',transfer_after_copy)
    uploaded=await http['bob'].post('/v1/handoffs/'+hid+'/result-uploads',params={
        'request_id':'source-transfer-result','confirmed':'true','expected_version':3,'shared_version':source['version'],'name':'失效成果.txt'},content=b'never-ready')
    assert uploaded.status_code==202,uploaded.text
    status=await http['alice'].get('/v1/handoffs/'+hid+'/result-publications/'+uploaded.json()['id'])
    assert status.json()['status']=='failed'
    assert (await http['tenant_admin'].get(path+'/materials')).json()['total']==0
    assert copied and all([not await objects.exists(key) for key in copied])


async def test_invalid_result_filename_is_client_error_without_intent(handoff_http,shared_id):
    """构造查询模型的校验异常不能变成500，也不得创建持久意图。"""
    http=handoff_http
    created=await submit(http,shared_id);hid=created.json()['id']
    rejected=await http['bob'].post('/v1/handoffs/'+hid+'/result-uploads',params={
        'request_id':'bad-result-name','confirmed':'true','expected_version':1,'shared_version':1,'name':'../escape.txt'},content=b'x')
    assert rejected.status_code==400,rejected.text
