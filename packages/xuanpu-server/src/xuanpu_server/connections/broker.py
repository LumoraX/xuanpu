"""逐调用核对员工、委托及绑定；迟到结果再次检查，不泄露撤权后资料。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.providers.wecom import WeComError, current_read_guard
from xuanpu_server.connections.rules import audit
from xuanpu_server.errors import NotFoundError, PermissionDeniedError
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from xuanpu_server.connections.models import ConnectionRef
    from xuanpu_server.connections.providers.wecom import WeComClient
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.connections.tokens import TokenBroker
    from xuanpu_server.tenancy import Principal


class CredentialBroker:
    """应用token不代表个人同意，来源与权限每次重算。"""
    def __init__(self,service: ConnectionService,tokens: TokenBroker,provider: WeComClient) -> None:
        """原生API与模型工具复用同一个出口。"""
        self.service,self.tokens,self.provider=service,tokens,provider

    async def read(self,who: Principal,cid: str,operation: str,parameters: dict[str,Any],
                   *,ref: ConnectionRef | None=None) -> dict[str,Any]:
        """参数不允许主体覆盖；只有明确核定的只读操作。"""
        if parameters or operation not in ('self_profile.read','directory_preview.read'):
            raise ValueError('只读操作不接受额外参数')
        administrative=operation=='directory_preview.read'
        tenant,user=await self.service.actor(who,admin=administrative)
        async with self.service.store.transaction(tenant) as state:
            install,current=self.service.reference(state,cid,user,operation,ref)
            corp=install['corp_id']
        identities=await self.service.identity.list_external_identities(tenant,'wecom',user_id=user,limit=1)
        if not identities or identities[0].attributes.get('corp_id')!=corp:
            raise PermissionDeniedError('请先绑定本人企业微信身份')
        userid=identities[0].attributes.get('userid')
        if not userid:
            raise NotFoundError('本人企业身份映射不完整')
        async def dispatch_guard() -> None:
            """同一逻辑读取的每个供应商请求（含目录下一页）均复核固定epoch。"""
            async with self.service.store.transaction(tenant) as state:
                await self.service.actor(who,admin=administrative)
                self.service.reference(state,cid,user,operation,current)
                audit(state,who,'read_dispatched',self.service.now(),installation_id=cid,
                      consent_id=current.consent_id,operation=operation,dispatch_id=new_id('read'))

        token=await self.tokens.get(tenant,cid)
        for attempt in range(2):
            await dispatch_guard()
            guard_token=current_read_guard.set(dispatch_guard)
            try:
                result=(await self.provider.profile(token,userid) if operation=='self_profile.read'
                        else await self.provider.directory(token))
                break
            except WeComError as exc:
                if attempt or exc.provider_code not in (40014,42001):
                    raise
                await dispatch_guard()
                token=await self.tokens.get(tenant,cid,invalidate=token)
            finally:
                current_read_guard.reset(guard_token)
        async with self.service.store.transaction(tenant) as state:
            await self.service.actor(who,admin=administrative)
            self.service.reference(state,cid,user,operation,current)
            audit(state,who,'read_completed',self.service.now(),installation_id=cid,
                  consent_id=current.consent_id,operation=operation)
        return {**result,'source':'wecom','read_at':self.service.now(),'connection_ref':current.model_dump()}
