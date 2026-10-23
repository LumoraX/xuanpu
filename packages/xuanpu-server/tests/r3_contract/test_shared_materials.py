"""共享副本有固定revision和SHA，不扩大原件归属，也不能通过旧URL绕过撤回。"""
from __future__ import annotations

import hashlib


async def test_publish_private_revision_keeps_independent_copy(shared_http, shared_id):
    """明确发布本人旧版本，共享后原件改版不覆盖副本。"""
    alice, bob = shared_http['alice'], shared_http['bob']
    uploaded = await alice.post('/v1/workspace/files', params={'name': '核对.txt', 'request_id': 'source-file'},
                                content=b'original-revision', headers={'Content-Type': 'text/plain'})
    assert uploaded.status_code == 200, uploaded.text
    file = uploaded.json()
    path = '/v1/shared-matters/' + shared_id
    created = await alice.post(path + '/materials', json={
        'request_id': 'publish-original', 'confirmed': True, 'expected_version': 1,
        'source_item_id': file['id'], 'source_revision': 1})
    assert created.status_code == 202, created.text
    publication = (await alice.get(path + '/publications/' + created.json()['id'])).json()
    assert publication['status'] == 'ready', publication
    material = publication['material_id']
    await alice.post(path + '/members', json={'request_id': 'material-reader', 'confirmed': True,
        'expected_version': 3, 'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer'})
    download = await bob.get(path + '/materials/' + material + '/content', params={'revision': 1})
    assert download.status_code == 200 and download.content == b'original-revision'
    assert download.headers['x-content-sha256'] == hashlib.sha256(download.content).hexdigest()
    assert (await bob.get('/v1/workspace/files/' + file['id'] + '/content')).status_code == 404
    changed = await alice.put('/v1/workspace/files/' + file['id'] + '/content',
        params={'request_id': 'source-revised', 'version': file['version']},
        content=b'new-original', headers={'Content-Type': 'text/plain'})
    assert changed.status_code == 200, changed.text
    assert (await bob.get(path + '/materials/' + material + '/content', params={'revision': 1})).content == b'original-revision'
    withdrawn = await alice.post(path + '/materials/' + material + '/withdraw', json={
        'request_id': 'withdraw', 'confirmed': True, 'expected_version': 4})
    assert withdrawn.status_code == 200, withdrawn.text
    assert (await bob.get(path + '/materials/' + material + '/content', params={'revision': 1})).status_code == 404


async def test_material_upload_replay_conflict_and_packet_scope(shared_http, shared_id):
    """上传回执稳定，限定资料成员只能下载明确选中的固定版本。"""
    alice, bob = shared_http['alice'], shared_http['bob']
    path = '/v1/shared-matters/' + shared_id
    query = {'name': '指定.txt', 'request_id': 'upload-one', 'confirmed': 'true', 'expected_version': 1}
    created = await alice.post(path + '/material-uploads', params=query, content=b'selected', headers={'Content-Type': 'text/plain'})
    assert created.status_code == 202, created.text
    assert (await alice.post(path + '/material-uploads', params=query, content=b'selected', headers={'Content-Type': 'text/plain'})).json() == created.json()
    assert (await alice.post(path + '/material-uploads', params=query, content=b'different', headers={'Content-Type': 'text/plain'})).status_code == 409
    first = (await alice.get(path + '/publications/' + created.json()['id'])).json()
    listing = (await alice.get(path + '/materials')).json()
    revision_id = listing['items'][0]['revisions'][0]['id']
    second = await alice.post(path + '/material-uploads', params={**query, 'name': '不可见.txt', 'request_id': 'upload-two', 'expected_version': 3}, content=b'not-in-packet')
    second = (await alice.get(path + '/publications/' + second.json()['id'])).json()
    grant = await alice.post(path + '/members', json={'request_id': 'packet-reader', 'confirmed': True,
        'expected_version': 5, 'subject_kind': 'user', 'subject_id': 'usr_bob', 'role': 'viewer',
        'scope': 'packet', 'packet_title': '待处理资料', 'material_revisions': [revision_id]})
    assert grant.status_code == 201, grant.text
    assert (await bob.get(path + '/materials/' + first['material_id'] + '/content', params={'revision': 1})).content == b'selected'
    assert (await bob.get(path + '/materials/' + second['material_id'] + '/content', params={'revision': 1})).status_code == 404
    visible = (await bob.get(path + '/materials')).json()
    assert visible['total'] == 1 and '不可见' not in str(visible)


async def test_material_publish_cannot_reference_another_employee(shared_http, shared_id):
    """请求中不能传source_user_id，猜中他人文件ID也不能发布。"""
    private = await shared_http['bob'].post('/v1/workspace/files', params={'name': '私人.txt', 'request_id': 'bob-file'}, content=b'private')
    path = '/v1/shared-matters/' + shared_id
    body = {'request_id': 'steal', 'confirmed': True, 'expected_version': 1,
            'source_item_id': private.json()['id'], 'source_revision': 1}
    assert (await shared_http['alice'].post(path + '/materials', json=body)).status_code == 404
    assert (await shared_http['alice'].post(path + '/materials', json={**body, 'source_user_id': 'usr_bob'})).status_code == 400
    assert (await shared_http['alice'].get(path)).json()['version'] == 1
