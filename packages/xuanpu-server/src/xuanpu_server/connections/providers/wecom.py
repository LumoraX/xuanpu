"""企微自建应用只读协议：现行auth/getuserinfo、固定目的端与白名单投影。"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx

from xuanpu_server.errors import PlatformError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

current_read_guard: ContextVar[Callable[[],Awaitable[None]] | None] = ContextVar('connection_read_guard',default=None)

API_BASE='https://qyapi.weixin.qq.com'
MAX_BYTES=1024*1024
_ALLOWED={'/cgi-bin/gettoken','/cgi-bin/auth/getuserinfo','/cgi-bin/user/get',
          '/cgi-bin/department/list','/cgi-bin/user/simplelist'}


class _CredentialLogFilter(logging.Filter):
    """httpx会记录请求URL，含认证参数时替换整条日志。"""
    def filter(self,record: logging.LogRecord) -> bool:
        """不在日志中保留token、secret、code或成员票据。"""
        if any(key in record.getMessage().lower() for key in
               ('access_token=','corpsecret=','code=','user_ticket=')):
            if record.name == 'uvicorn.access':
                # 原生访问格式器依赖五个参数；仅隐藏路径，保留方法与状态。
                if not isinstance(record.args, tuple) or len(record.args) != 5:
                    # 未知格式含认证值时不输出，避免格式器异常回显原始记录。
                    return False
                client, method, _, version, status = record.args
                record.msg = '%s - "%s %s HTTP/%s" %d'
                record.args = (client, method, '/[connection-auth-redacted]', version, status)
            else:
                record.msg='企业连接HTTP请求（认证URL已隐去）'
                record.args=()
        return True


class WeComError(PlatformError):
    """供应商失败只保留错误码，不携带URL、正文或secret。"""
    code='connection_provider_error'
    status=502

    def __init__(self,provider_code: int=-1) -> None:
        """用稳定错误码供上层核定是否刷新token。"""
        super().__init__('企业微信只读请求失败，请核对应用权限或稍后重试')
        self.provider_code=provider_code


class WeComClient:
    """固定HTTPS出口；测试只能通过显式构造注入transport。"""
    def __init__(self,*,transport: httpx.AsyncBaseTransport | None=None) -> None:
        """不从安装或业务包接受任意基地址。"""
        for name in ('httpx','uvicorn.access','httpcore.http11','httpcore.http2'):
            logger=logging.getLogger(name)
            if not any(isinstance(f,_CredentialLogFilter) for f in logger.filters):
                logger.addFilter(_CredentialLogFilter())
        self._client=httpx.AsyncClient(base_url=API_BASE,transport=transport,
            timeout=httpx.Timeout(15,connect=5),follow_redirects=False,trust_env=False)

    async def get(self,path: str,parameters: dict[str,str]) -> dict[str,Any]:
        """只允许冻结的GET路径，限制读取大小，不回显供应商错误。"""
        if path not in _ALLOWED:
            raise ValueError('未核定企业微信接口')
        guard=current_read_guard.get()
        if guard is not None:
            await guard()
        try:
            async with self._client.stream('GET',path,params=parameters) as response:
                if response.status_code!=200:
                    raise WeComError()
                raw=bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>MAX_BYTES:
                        raise WeComError()
            data=json.loads(raw)
        except (httpx.HTTPError,ValueError):
            raise WeComError() from None
        if not isinstance(data,dict) or type(data.get('errcode')) is not int:
            raise WeComError()
        if data['errcode']!=0:
            raise WeComError(data['errcode'])
        return data

    async def token(self,corp: str,secret: str) -> tuple[str,int]:
        """每个应用独立token，不是个人refresh_token。"""
        data=await self.get('/cgi-bin/gettoken',{'corpid':corp,'corpsecret':secret})
        token,ttl=data.get('access_token'),data.get('expires_in')
        if not isinstance(token,str) or not 1<=len(token)<=2048 or type(ttl) is not int or not 60<=ttl<=86400:
            raise WeComError()
        return token,ttl

    async def identity(self,token: str,code: str) -> str:
        """仅接受本企业成员；互联/上下游和外部联系人另行立项。"""
        if not 1<=len(code)<=512:
            raise ValueError('授权码长度非法')
        data=await self.get('/cgi-bin/auth/getuserinfo',{'access_token':token,'code':code})
        uid=data.get('userid')
        if not isinstance(uid,str) or not 1<=len(uid.encode())<=64 or '/' in uid:
            raise WeComError()
        return uid

    async def profile(self,token: str,userid: str) -> dict[str,Any]:
        """只返回核定的基础资料；缺失姓名不替换成企业身份。"""
        raw=await self.get('/cgi-bin/user/get',{'access_token':token,'userid':userid})
        if not isinstance(raw.get('userid'),str) or raw['userid'].lower()!=userid.lower():
            raise WeComError()
        allowed: dict[str,Any]={'userid':raw['userid']}
        for key in ('name','position'):
            value=raw.get(key)
            if isinstance(value,str) and len(value)<=256:
                allowed[key]=value
        departments=raw.get('department')
        if isinstance(departments,list) and len(departments)<=1000 and all(type(v) is int for v in departments):
            allowed['department']=departments
        return {'data':allowed,'missing_fields':[k for k in ('name','position','department') if k not in allowed]}

    async def directory(self,token: str) -> dict[str,Any]:
        """仅预览应用可见部门和成员，不推断全企业完整性。"""
        raw=await self.get('/cgi-bin/department/list',{'access_token':token})
        departments=raw.get('department')
        if not isinstance(departments,list) or len(departments)>100:
            raise WeComError()
        result=[]
        for department in departments:
            if not isinstance(department,dict) or type(department.get('id')) is not int:
                raise WeComError()
            item: dict[str,Any]={'id':department['id']}
            if isinstance(department.get('name'),str) and len(department['name'])<=256:
                item['name']=department['name']
            if type(department.get('parentid')) is int:
                item['parentid']=department['parentid']
            members=await self.get('/cgi-bin/user/simplelist',{'access_token':token,
                'department_id':str(department['id']),'fetch_child':'0'})
            users=members.get('userlist')
            if not isinstance(users,list) or len(users)>1000:
                raise WeComError()
            projected=[]
            for user in users:
                if not isinstance(user,dict) or not isinstance(user.get('userid'),str) or not 1<=len(user['userid'].encode())<=64:
                    raise WeComError()
                member: dict[str,Any]={'userid':user['userid']}
                if isinstance(user.get('name'),str) and len(user['name'])<=256:
                    member['name']=user['name']
                groups=user.get('department')
                if isinstance(groups,list) and len(groups)<=1000 and all(type(v) is int for v in groups):
                    member['department']=groups
                projected.append(member)
            item['members']=projected
            result.append(item)
        return {'departments':result,'scope':'application_visible','complete':False,
                'completeness_reason':'企业应用可见范围不等于全企业；此处不授权缺失成员停用'}

    async def close(self) -> None:
        """关闭HTTP资源。"""
        await self._client.aclose()
