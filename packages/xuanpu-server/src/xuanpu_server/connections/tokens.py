"""按应用和密钥修订缓存token；短租约CAS，供应商请求在事务外。"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.credential_vault import cipher, scope
from xuanpu_server.connections.rules import get
from xuanpu_server.errors import ConflictError, PermissionDeniedError
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.secrets import SecretVault


class TokenBroker:
    """不持有明文长期缓存，密钥改变后不会复用旧应用token。"""
    def __init__(self,service: ConnectionService,vault: SecretVault,provider: WeComClient) -> None:
        """复用安装存储、保管库与只读协议客户端。"""
        self.service,self.vault,self.provider=service,vault,provider

    async def installation(self,tenant: str,cid: str) -> dict[str,Any]:
        """每次使用前核对租户、灰度及安装，不能仅依赖旧token。"""
        company=await self.service.control.get_tenant(tenant)
        if company is None or company.status!='active' or not self.service.config.allows(tenant):
            raise PermissionDeniedError('企业连接已停用')
        async with self.service.store.transaction(tenant) as state:
            record=dict(get(state,cid,'installation'))
            self.service.active(record)
            return record

    async def get(self,tenant: str,cid: str,*,invalidate: str | None=None) -> str:
        """仅失效的旧token被CAS清理，其他调用刷新出的新token保持有效。"""
        installation=await self.installation(tenant,cid)
        secret=await self.vault.get(tenant,installation['secret_ref'])
        revision=hashlib.sha256(secret.encode()).hexdigest()
        key='token:'+cid+':'+revision
        aad=scope(cid,'application','access_token',revision)
        owner=new_id('refresh')
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            async with self.service.store.transaction(tenant) as state:
                record=state.records.setdefault(key,{'kind':'token','expires_at':0.,'owner':'','lease_until':0.})
                encrypted=state.credentials.get(key)
                if encrypted is not None and record['expires_at']>self.service.now()+60:
                    token=cipher(self.vault).open_scoped(tenant,aad,encrypted)
                    if invalidate is None or token!=invalidate:
                        return token
                    record['expires_at']=0.
                if record['lease_until']<=self.service.now():
                    record.update(owner=owner,lease_until=self.service.now()+20)
                    break
            await asyncio.sleep(.02)
        else:
            raise ConflictError('企业连接令牌正在更新，请稍后重试')
        try:
            token,ttl=await self.provider.token(installation['corp_id'],secret)
            await self.installation(tenant,cid)
            current_secret=await self.vault.get(tenant,installation['secret_ref'])
            if hashlib.sha256(current_secret.encode()).hexdigest()!=revision:
                raise ConflictError('应用密钥已变化，请重试读取')
            async with self.service.store.transaction(tenant) as state:
                record=get(state,key,'token')
                if record['owner']!=owner or record['lease_until']<=self.service.now():
                    raise ConflictError('令牌更新租约已失效')
                current=get(state,cid,'installation')
                self.service.active(current)
                if current['version']!=installation['version']:
                    raise ConflictError('应用安装已变化')
                state.credentials[key]=cipher(self.vault).seal_scoped(tenant,aad,token)
                record.update(expires_at=self.service.now()+ttl,owner='',lease_until=0.)
            return token
        finally:
            async with self.service.store.transaction(tenant) as state:
                remaining=state.records.get(key)
                if remaining is not None and remaining.get('owner')==owner:
                    remaining.update(owner='',lease_until=0.)
