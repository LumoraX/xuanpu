"""企微客户端网页授权：随机state、同浏览器绑定与一次性持久回调。"""
from __future__ import annotations

import hmac
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from xuanpu_server.connections.account_bindings import subject
from xuanpu_server.errors import AuthenticationError, ConflictError, PermissionDeniedError
from xuanpu_server.identity.cookies import login_token_hash, new_login_token
from xuanpu_server.identity.models import ExternalIdentity, LoginSession
from xuanpu_server.identity.urls import safe_return_to

if TYPE_CHECKING:
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.connections.tokens import TokenBroker
    from xuanpu_server.identity.service import IdentitySettings
    from xuanpu_server.tenancy import Principal


class WeComLogin:
    """只绑定经过本应用身份交换并实际可读的企业成员；不JIT建人。"""
    def __init__(self,service: ConnectionService,tokens: TokenBroker,provider: WeComClient,
                 settings: IdentitySettings) -> None:
        """共享连接域与既有登录会话，不创建第二套平台身份。"""
        self.service,self.tokens,self.provider,self.settings=service,tokens,provider,settings

    async def start(self,tenant: str,cid: str,return_to: str,*,who: Principal | None=None) -> dict[str,Any]:
        """发起客户端网页授权；返回路径只允许本站，PKCE不假定受支持。"""
        install=await self.tokens.installation(tenant,cid)
        if self.settings.public_url is None:
            raise ConflictError('请先配置玄圃public_url和企微可信域名')
        target=None
        if who is not None:
            own,user=await self.service.actor(who,browser=True)
            if own!=tenant:
                raise PermissionDeniedError('不能跨租户绑定')
            target=user
        state=secrets.token_hex(32)
        cookie=secrets.token_urlsafe(32)
        await self.service.store.put_attempt(login_token_hash(state),{
            'tenant_id':tenant,'connection_id':cid,'installation_version':install['version'],
            'corp_id':install['corp_id'],'target_user':target,'cookie_hash':login_token_hash(cookie),
            'return_to':safe_return_to(return_to,fallback='/console/'),'expires_at':self.service.now()+300},now=self.service.now())
        query=urlencode({'appid':install['corp_id'],'agentid':install['agent_id'],
            'redirect_uri':self.settings.public_url+'/v1/auth/wecom/callback',
            'response_type':'code','scope':'snsapi_base','state':state})
        return {'url':'https://open.weixin.qq.com/connect/oauth2/authorize?'+query+'#wechat_redirect',
                'cookie':cookie}

    async def callback(self,state: str,code: str,cookie: str | None,
                       previous_login: str | None) -> dict[str,Any]:
        """消费state后再接供应商，失败也不能重放授权码。"""
        if not re.fullmatch('[a-f0-9]{64}',state):
            raise AuthenticationError('企业微信登录状态无效')
        attempt=await self.service.store.take_attempt(login_token_hash(state),now=self.service.now())
        if attempt is None or cookie is None or not hmac.compare_digest(login_token_hash(cookie),attempt['cookie_hash']):
            raise AuthenticationError('企业微信登录状态无效或已过期')
        tenant,cid=attempt['tenant_id'],attempt['connection_id']
        if attempt['target_user'] is not None:
            current_login=(None if previous_login is None else await self.service.identity.get_login_session(
                login_token_hash(previous_login),now=self.service.now()))
            if current_login is None or (current_login.tenant_id,current_login.user_id)!=(tenant,attempt['target_user']):
                raise AuthenticationError('绑定期间登录身份已变化，请重新开始')
        install=await self.tokens.installation(tenant,cid)
        if install['version']!=attempt['installation_version']:
            raise ConflictError('应用配置已变化，请重新登录')
        token=await self.tokens.get(tenant,cid)
        userid=await self.provider.identity(token,code)
        # 识别成员并不证明成员在应用可见范围内；读取本人基础资料再确认。
        await self.provider.profile(token,userid)
        user=await self._mapped_user(attempt,userid)
        latest=await self.tokens.installation(tenant,cid)
        if latest['version']!=attempt['installation_version']:
            raise ConflictError('应用配置已变化，请重新登录')
        current=await self.service.identity.get_user(tenant,user)
        if current is None or current.status!='active':
            raise PermissionDeniedError('员工不可用')
        token,hashed=new_login_token()
        now,ttl=self.service.now(),self.settings.session_ttl_seconds
        identity=ExternalIdentity(tenant,'wecom',subject(install['corp_id'],userid),user,
                                  {'corp_id':install['corp_id'],'userid':userid})
        await self.service.identity.complete_external_login(identity,LoginSession(hashed,tenant,user,now,now+ttl,now),
            bind=attempt['target_user'] is not None,connection_id=cid,
            connection_version=attempt['installation_version'],
            required_login_hash=login_token_hash(previous_login) if previous_login is not None and attempt['target_user'] is not None else None)
        if previous_login is not None:
            old=await self.service.identity.get_login_session(login_token_hash(previous_login),now=now)
            if old is not None:
                await self.service.identity.delete_login_session(old.tenant_id,old.token_hash)
        return {'return_to':attempt['return_to'],'session_token':token,'max_age':ttl}

    async def _mapped_user(self,attempt: dict[str,Any],userid: str) -> str:
        """预配或当前本人明确绑定，绝不按姓名创建或合并。"""
        tenant=attempt['tenant_id']
        external=subject(attempt['corp_id'],userid)
        user=await self.service.identity.get_user_by_identity(tenant,'wecom',external)
        target=attempt['target_user']
        if target is not None:
            current=await self.service.identity.get_user(tenant,target)
            if current is None or current.status!='active':
                raise PermissionDeniedError('员工不可用')
            if user is not None and user.user_id!=target:
                raise ConflictError('企业身份已绑定其他员工')
            return str(target)
        if user is None:
            raise PermissionDeniedError('请先由管理员明确映射已有员工，或由员工登录后绑定')
        return user.user_id
