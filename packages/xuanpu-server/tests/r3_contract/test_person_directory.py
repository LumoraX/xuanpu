"""员工选择不依赖部门，不提供私人资料或管理员职权。"""
from __future__ import annotations


async def test_people_without_departments_are_selectable(organization_http, r3_world):
    """尚无任职的员工可以直接协作，返回最小姓名和稳定编号。"""
    response = await organization_http['alice'].get('/v1/organization/people', params={'limit': 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['total'] == 3 and len(body['items']) == 1
    assert set(body['items'][0]) == {'user_id', 'display_name'}
    second = await organization_http['alice'].get('/v1/organization/people', params={'limit': 1, 'cursor': body['next_cursor']})
    assert second.json()['items'][0]['user_id'] != body['items'][0]['user_id']
    assert second.json()['total'] == 3
    assert (await organization_http['machine'].get('/v1/organization/people')).status_code == 403
    await r3_world.platform.identity.store.set_user_status('acme', 'usr_bob', 'disabled', reason='admin', at=100)
    remaining = (await organization_http['alice'].get('/v1/organization/people')).json()
    assert remaining['total'] == 2
    assert all(p['user_id'] != 'usr_bob' for p in remaining['items'])


async def test_people_are_tenant_isolated(organization_http, r3_beta):
    """另一企业只能枚举本企业员工，ID相同不合并。"""
    response = await r3_beta['alice'].get('/v1/organization/people')
    assert response.status_code == 200
    assert response.json()['total'] == 2
    assert all(p['display_name'] == '同名员工' for p in response.json()['items'])
