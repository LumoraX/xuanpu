"""R3双后端HTTP夹具；供应商模拟响应不作为真实企微证据。"""
from __future__ import annotations

import time

import httpx
import pytest
from xuanpu_server.api.app import create_app
from xuanpu_server.connections.models import ConnectionConfig
from xuanpu_server.identity.config import IdentityConfig
from xuanpu_server.identity.cookies import login_token_hash
from xuanpu_server.identity.models import LoginSession, UserRecord
from xuanpu_server.identity.service import IdentitySettings


def _cookies(token):
    """模拟真实浏览器的主机Cookie，避免测试工具域为空导致重复Cookie。"""
    jar=httpx.Cookies()
    jar.set('__Host-xuanpu_session',token,domain='office.test',path='/')
    return jar


@pytest.fixture
async def r3_world(identity_stores, world_opener, local_sandboxes):
    """复用真实平台装配、Cookie认证与同后端持久化。"""
    control, identity = identity_stores
    settings = IdentitySettings.from_config('https://office.test', IdentityConfig())
    world = await world_opener(store=control, identity_store=identity,
                               identity_settings=settings, sandboxes=local_sandboxes(),
                               connections=ConnectionConfig(enabled=True,tenants=('acme',)))
    try:
        yield world
    finally:
        await world.platform.close()


@pytest.fixture
async def r3_http(r3_world):
    """Alice、Bob及管理员用独立浏览器会话，机器用普通API key。"""
    platform = r3_world.platform
    now = time.time()
    clients = {}
    for key, role in [('alice','tenant_member'),('bob','tenant_member'),('tenant_admin','tenant_admin')]:
        uid = 'usr_' + key
        await platform.identity.store.create_user(UserRecord(tenant_id='acme',user_id=uid,
                    display_name='同名员工',created_at=now,role=role))
        token = 'test-session-' + key
        await platform.identity.store.create_login_session(LoginSession(
            token_hash=login_token_hash(token),tenant_id='acme',user_id=uid,created_at=now,
            expires_at=now+3600,last_seen_at=now))
        clients[key] = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform)),
            base_url='https://office.test',cookies=_cookies(token),
            headers={'Origin':'https://office.test','X-Requested-By':'xuanpu'})
    clients['machine'] = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform)),
        base_url='https://office.test',headers={'Authorization':'Bearer '+r3_world.member_key})
    try:
        yield clients
    finally:
        for client in clients.values():
            await client.aclose()


@pytest.fixture
async def r3_connected(r3_http,r3_world):
    """经产品API完成安装、账号预配与本人同意，返回固定引用。"""
    result=await r3_http['tenant_admin'].post('/v1/admin/connections',json={
        'request_id':'fixture-install','name':'企微只读','corp_id':'corp-a','agent_id':'1',
        'secret_ref':'wecom-secret','operations':['self_profile.read','directory_preview.read']})
    assert result.status_code==201,result.text
    cid=result.json()['id']
    result=await r3_http['tenant_admin'].patch(f'/v1/admin/connections/{cid}',json={
        'request_id':'fixture-enable','expected_version':1,'status':'active',
        'capability_evidence':'test fixture only; real acceptance not_checked'})
    assert result.status_code==200,result.text
    for key in ('alice','tenant_admin'):
        result=await r3_http['tenant_admin'].post(f'/v1/admin/connections/{cid}/accounts',json={
            'user_id':'usr_'+key,'userid':key})
        assert result.status_code==201,result.text
    result=await r3_http['alice'].post(f'/v1/me/connections/{cid}/consents',json={
        'request_id':'fixture-consent','operations':['self_profile.read'],
        'purpose':'本人资料核对','expires_at':time.time()+3600})
    assert result.status_code==201,result.text
    await r3_world.platform.vault.put('acme','wecom-secret','secret',actor='test')
    return cid,result.json()


@pytest.fixture
async def r3_package(r3_world):
    """管理员绑定必须指向已校验并发布的真实包。"""
    from pathlib import Path

    from xuanpu_spec import pack_package
    root=Path(__file__).resolve().parents[4]/'examples/wecom-readonly'
    return await r3_world.platform.packages.publish(r3_world.admin,pack_package(root),activate=True)


@pytest.fixture
async def r3_beta(r3_world,r3_http):
    """第二企业复用相同本地usr与显示名，隔离仍由真实租户及企业绑定决定。"""
    from xuanpu_server.tenancy import Tenant
    platform=r3_world.platform
    old=platform.connections.config
    platform.connections.config=ConnectionConfig(enabled=True,tenants=('acme','beta'))
    await platform.store.create_tenant(Tenant('beta','第二企业',created_at=time.time()))
    clients={}
    for name,role in [('alice','tenant_member'),('tenant_admin','tenant_admin')]:
        user='usr_'+name;token='beta-session-'+name;now=time.time()
        await platform.identity.store.create_user(UserRecord('beta',user,'同名员工',now,role=role))
        await platform.identity.store.create_login_session(LoginSession(login_token_hash(token),'beta',user,now,now+3600,now))
        clients[name]=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(platform)),
            base_url='https://office.test',cookies=_cookies(token),
            headers={'Origin':'https://office.test','X-Requested-By':'xuanpu'})
    try:
        yield clients
    finally:
        platform.connections.config=old
        for client in clients.values():
            await client.aclose()


@pytest.fixture
async def organization_http(r3_http, r3_world):
    """开启测试租户灰度；生产默认配置仍关闭。"""
    assert hasattr(r3_world.platform, 'organization'), '尚未装配组织服务'
    from xuanpu_server.organization.config import OrganizationConfig

    r3_world.platform.organization.config = OrganizationConfig(enabled=True, tenants=('acme',))
    return r3_http


@pytest.fixture
async def organization_ids(organization_http):
    """通过同一产品API建立两个部门与一个子部门。"""
    admin = organization_http['tenant_admin']
    ids = {}
    for key, parent in (('a', None), ('b', None), ('child', 'a')):
        result = await admin.post('/v1/admin/organization/units', json={
            'request_id': 'seed-unit-' + key, 'name': key,
            'parent_id': ids.get(parent)})
        assert result.status_code == 201, result.text
        ids[key] = result.json()['id']
    return ids


@pytest.fixture
async def organization_source(organization_http, r3_connected, r3_world):
    """模拟供应商仅作为自动化故障证据，绝不替代真实目录验收。"""
    from xuanpu_server.connections.providers.wecom import WeComClient

    state = {'name': '供应商研发', 'fail_page': False, 'extra_user': False, 'employee_name': '同名员工'}
    async def handler(request):
        """数据可由测试在预览和应用间改变。"""
        if request.url.path.endswith('/gettoken'):
            value = {'access_token': 'synthetic-token', 'expires_in': 7200}
        elif request.url.path.endswith('/department/list'):
            value = {'department': [{'id': 1, 'name': state['name'], 'parentid': 0},
                                    {'id': 2, 'name': '二组', 'parentid': 1}]}
        elif request.url.path.endswith('/user/simplelist'):
            if state['fail_page'] and request.url.params['department_id'] == '2':
                return httpx.Response(200, json={'errcode': 60011})
            users = [{'userid': 'alice', 'name': state['employee_name'], 'department': [1]}]
            if state['extra_user']:
                users.append({'userid': 'unmapped', 'name': '同名员工', 'department': [1]})
            value = {'userlist': users}
        else:
            raise AssertionError('非只读目录请求')
        return httpx.Response(200, json={'errcode': 0, **value})
    platform = r3_world.platform
    original = platform.wecom_provider
    provider = WeComClient(transport=httpx.MockTransport(handler))
    platform.wecom_provider = provider
    platform.connection_tokens.provider = provider
    platform.connection_broker.provider = provider
    platform.wecom_login.provider = provider
    await original.close()
    cid, _ = r3_connected
    response = await organization_http['tenant_admin'].post(f'/v1/me/connections/{cid}/consents', json={
        'request_id': 'directory-consent', 'operations': ['directory_preview.read'],
        'purpose': '组织差异预览测试', 'expires_at': time.time() + 3600})
    assert response.status_code == 201, response.text
    return cid, state


@pytest.fixture
async def shared_http(organization_http, r3_world):
    """共享业务使用明确员工及同后端身份，不模拟越权成功。"""
    assert hasattr(r3_world.platform, 'shared_matters'), '尚未装配显式共享事项'
    from xuanpu_server.shared_matters.models import SharedMatterConfig

    r3_world.platform.shared_matters.config = SharedMatterConfig(enabled=True, tenants=('acme',))
    return organization_http


@pytest.fixture
async def shared_id(shared_http):
    """通过产品API创建明确事项，不借用个人事项ID。"""
    response = await shared_http['alice'].post('/v1/shared-matters', json={
        'request_id': 'shared-create', 'confirmed': True, 'title': '共同核对材料', 'content': '仅共享事项正文'})
    assert response.status_code == 201, response.text
    return response.json()['id']
