"""真实HTTP适配的只读、业务错误及最小投影。"""
from __future__ import annotations

import json

import httpx
import pytest


async def test_provider_rejects_200_business_error():
    """HTTP成功不等于企微成功，错误正文不透传。"""
    from xuanpu_server.connections.providers.wecom import WeComClient, WeComError
    async def handler(request):
        return httpx.Response(200,json={'errcode':60011,'errmsg':'sensitive-token'})
    client=WeComClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WeComError) as error:
            await client.profile('token','alice')
        assert 'sensitive-token' not in str(error.value)
        assert error.value.provider_code==60011
    finally:
        await client.close()


async def test_projection_does_not_invent_or_leak_fields():
    """真实返回白名单之外的敏感字段全部排除，姓名缺失不补造。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    async def handler(request):
        return httpx.Response(200,json={'errcode':0,'userid':'alice','mobile':'secret-phone',
             'access_token':'secret-token','user_ticket':'secret-ticket','department':[1]})
    client=WeComClient(transport=httpx.MockTransport(handler))
    try:
        result=await client.profile('token','alice')
        text=json.dumps(result)
        assert all(secret not in text for secret in ('secret-phone','secret-token','secret-ticket'))
        assert 'name' in result['missing_fields']
        assert 'name' not in result['data']
    finally:
        await client.close()


async def test_no_redirect_or_malformed_identity():
    """重定向和外部联系人不能成为员工绑定。"""
    from xuanpu_server.connections.providers.wecom import WeComClient, WeComError
    requests=[]
    async def handler(request):
        requests.append(request)
        if request.url.path.endswith('getuserinfo'):
            return httpx.Response(200,json={'errcode':0,'openid':'external-only'})
        return httpx.Response(302,headers={'Location':'https://evil.test/'})
    client=WeComClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(WeComError):
            await client.profile('token','alice')
        with pytest.raises(WeComError):
            await client.identity('token','code')
        assert len(requests)==2
    finally:
        await client.close()


async def test_directory_does_not_echo_nested_unexpected_fields():
    """字段名允许也不能透传不合契约的嵌套私密正文。"""
    from xuanpu_server.connections.providers.wecom import WeComClient
    async def handler(request):
        value=({'department':[{'id':1,'name':{'mobile':'private-phone'},'parentid':0}]}
            if request.url.path.endswith('/department/list') else
            {'userlist':[{'userid':'alice','name':{'email':'private-email'},'department':[1]}]})
        return httpx.Response(200,json={'errcode':0,**value})
    client=WeComClient(transport=httpx.MockTransport(handler))
    try:
        result=await client.directory('token')
        assert 'private-' not in json.dumps(result)
    finally:
        await client.close()


async def test_authentication_urls_are_removed_from_http_and_access_logs(caplog):
    """供应商请求与OAuth回调日志不包含示例认证值。"""
    import logging

    from xuanpu_server.connections.providers.wecom import WeComClient
    async def handler(request):
        return httpx.Response(200,json={'errcode':0,'access_token':'token-private','expires_in':7200})
    client=WeComClient(transport=httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.DEBUG):
            await client.token('corp','secret-private')
            logging.getLogger('uvicorn.access').info('GET /v1/auth/wecom/callback?code=code-private&state=state')
        assert 'secret-private' not in caplog.text
        assert 'code-private' not in caplog.text
    finally:
        await client.close()
