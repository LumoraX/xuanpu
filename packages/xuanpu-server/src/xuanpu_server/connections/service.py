"""所有入口共用的连接安装、本人委托与当前权限检查。"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from xuanpu_server.connections.models import ConnectionConfig, ConnectionRef
from xuanpu_server.connections.rules import (
    audit,
    get,
    public_consent,
    public_install,
    remember,
    replay,
)
from xuanpu_server.errors import ConflictError, NotFoundError, PermissionDeniedError
from xuanpu_server.ids import new_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from xuanpu_server.connections.models import (
        ChangeInput,
        ConsentInput,
        InstallInput,
        RevokeInput,
    )
    from xuanpu_server.connections.store import ConnectionState, ConnectionStore
    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.tenancy import Principal


class ConnectionService:
    """凭据与授权结论均不向模型或浏览器透传。"""
    def __init__(self,store: ConnectionStore,control: ControlPlaneStore,identity: IdentityStore,
                 *,config: ConnectionConfig | None=None,now: Callable[[],float]=time.time) -> None:
        """注入后端、灰度及统一时钟。"""
        self.store,self.control,self.identity=store,control,identity
        self.packages: PackageRegistry | None = None
        self.config=config or ConnectionConfig()
        self.now=now

    async def actor(self,who: Principal,*,browser: bool=False,admin: bool=False,
                    allow_disabled: bool=False) -> tuple[str,str]:
        """员工当前启用；同意仅接受本人浏览器，act-as无权创建同意。"""
        tenant=who.require_tenant_admin() if admin else who.require_tenant()
        if not allow_disabled and not self.config.allows(tenant):
            raise PermissionDeniedError('员工连接尚未对本租户启用')
        company=await self.control.get_tenant(tenant)
        if company is None or company.status!='active':
            raise PermissionDeniedError('租户不可用')
        if not admin and not who.user_id:
            raise PermissionDeniedError('这个操作需要员工身份')
        if browser and (who.key_id is not None or who.user_id is None):
            raise PermissionDeniedError('请由员工本人在浏览器确认授权')
        live_role=who.role
        if who.user_id:
            user=await self.identity.get_user(tenant,who.user_id)
            if user is None or user.status!='active':
                raise PermissionDeniedError('员工不可用')
            from xuanpu_server.tenancy import lower_role
            live_role=lower_role(live_role,user.role)
        if who.key_id is not None:
            key=await self.control.get_api_key(who.key_id)
            if (key is None or key.tenant_id!=tenant or not key.active
                or (who.user_id is not None and not key.can_act_as_user)):
                raise PermissionDeniedError('调用凭据已失效或不能代员工调用')
            from xuanpu_server.tenancy import lower_role
            live_role=lower_role(live_role,key.role)
        if admin and live_role!='tenant_admin':
            raise PermissionDeniedError('当前管理员权限已失效')
        return tenant,who.user_id or ''

    async def install(self,who: Principal,body: InstallInput) -> dict[str,Any]:
        """创建禁用安装，核定真实应用权限后才可启用。"""
        tenant,_=await self.actor(who,admin=True)
        key='install:'+who.actor+':'+body.request_id
        async with self.store.transaction(tenant) as state:
            old=replay(state,key,body.model_dump())
            if old is not None:
                return old
            if sum(r.get('kind')=='installation' for r in state.records.values())>=100:
                raise ConflictError('安装数已达上限')
            identifier=new_id('cnx')
            record={'kind':'installation','id':identifier,'provider':'wecom','status':'disabled',
                    'version':1,'credential_revision':1,'capability_status':'unverified',
                    **body.model_dump(exclude={'request_id'})}
            state.records[identifier]=record
            result=public_install(record)
            remember(state,key,body.model_dump(),result)
            audit(state,who,'installed',self.now(),installation_id=identifier)
            return result

    async def change(self,who: Principal,cid: str,body: ChangeInput) -> dict[str,Any]:
        """状态改变递增安装版本，使旧授权引用失效。"""
        tenant,_=await self.actor(who,admin=True,allow_disabled=body.status=='disabled')
        async with self.store.transaction(tenant) as state:
            record=get(state,cid,'installation')
            key='change:'+who.actor+':'+cid+':'+body.request_id
            old=replay(state,key,body.model_dump())
            if old is not None:
                return old
            if record['status']=='disabled' and body.status=='disabled':
                return public_install(record)
            if record['version']!=body.expected_version:
                raise ConflictError('安装版本已变化')
            if body.status=='active' and not body.capability_evidence.strip():
                raise ConflictError('须先核定真实应用权限及提供证据引用')
            record.update(status=body.status,version=record['version']+1,
                          capability_status='verified' if body.status=='active' else record['capability_status'],
                          capability_evidence=body.capability_evidence)
            result=public_install(record)
            remember(state,key,body.model_dump(),result,security=body.status=='disabled')
            audit(state,who,'installation_changed',self.now(),installation_id=cid,version=record['version'])
            return result

    async def listing(self,who: Principal,*,admin: bool=False) -> dict[str,Any]:
        """只显示安装元数据与本人的同意，关闭后仍可查看并撤销。"""
        tenant,user=await self.actor(who,admin=admin,allow_disabled=True)
        async with self.store.transaction(tenant) as state:
            rows=[]
            for value in state.records.values():
                if value.get('kind')!='installation':
                    continue
                row=public_install(value)
                row['consents']=[public_consent(c,self.now()) for c in state.records.values()
                    if c.get('kind')=='consent' and c['installation_id']==value['id'] and c['user_id']==user]
                rows.append(row)
            return {'enabled':self.config.allows(tenant),'connections':rows}

    async def consent(self,who: Principal,cid: str,body: ConsentInput) -> dict[str,Any]:
        """创建有限同意；撤销后的原请求不能重放成有效同意。"""
        tenant,user=await self.actor(who,browser=True)
        async with self.store.transaction(tenant) as state:
            installation=get(state,cid,'installation')
            self.active(installation)
            key='consent:'+user+':'+cid+':'+body.request_id
            old=replay(state,key,body.model_dump())
            if old is not None:
                live=get(state,old['consent_id'],'consent')
                if live['status']!='active' or live['expires_at']<=self.now():
                    raise ConflictError('原授权已失效，请重新确认')
                self.valid_consent(live,installation,user)
                return public_consent(live,self.now())
            if not self.now()<body.expires_at<=self.now()+self.config.max_consent_seconds:
                raise ValueError('授权有效期须在当前时间之后且不超过配置上限')
            if not set(body.operations)<=set(installation['operations']):
                raise PermissionDeniedError('超出应用核定操作范围')
            if 'directory_preview.read' in body.operations:
                await self.actor(who,admin=True)
            identifier=new_id('consent')
            record={'kind':'consent','consent_id':identifier,'installation_id':cid,
                'installation_version':installation['version'],'user_id':user,
                'version':1,'epoch':1,'status':'active',**body.model_dump(exclude={'request_id'})}
            state.records[identifier]=record
            result=public_consent(record,self.now())
            remember(state,key,body.model_dump(),result)
            audit(state,who,'consented',self.now(),installation_id=cid,consent_id=identifier)
            return result

    async def consent_get(self,who: Principal,cid: str,consent_id: str) -> dict[str,Any]:
        """只能取本人同意详情。"""
        tenant,user=await self.actor(who,allow_disabled=True)
        async with self.store.transaction(tenant) as state:
            record=get(state,consent_id,'consent')
            if record['user_id']!=user or record['installation_id']!=cid:
                raise NotFoundError('授权记录不存在')
            return public_consent(record,self.now())

    async def revoke(self,who: Principal,cid: str,consent_id: str,body: RevokeInput) -> dict[str,Any]:
        """撤销提交后，所有新派发与迟到结果必须重新验证epoch。"""
        tenant,user=await self.actor(who,allow_disabled=True)
        async with self.store.transaction(tenant) as state:
            record=get(state,consent_id,'consent')
            if record['user_id']!=user or record['installation_id']!=cid:
                raise NotFoundError('授权记录不存在')
            key='revoke:'+user+':'+consent_id+':'+body.request_id
            old=replay(state,key,body.model_dump())
            if old is not None:
                return old
            if record['status']=='revoked':
                return public_consent(record,self.now())
            if record['version']!=body.expected_version:
                raise ConflictError('授权版本已变化')
            record.update(status='revoked',version=record['version']+1,epoch=record['epoch']+1)
            result=public_consent(record,self.now())
            remember(state,key,body.model_dump(),result,security=True)
            audit(state,who,'revoked',self.now(),installation_id=cid,consent_id=consent_id,epoch=record['epoch'])
            return result

    @staticmethod
    def active(installation: dict[str,Any]) -> None:
        """应用未激活或未核定则不发出请求。"""
        if installation['status']!='active' or installation['capability_status']!='verified':
            raise ConflictError('连接未启用或应用能力尚未核定')

    def valid_consent(self,consent: dict[str,Any],installation: dict[str,Any],user: str) -> None:
        """当前委托必须与当前安装修订、员工和时间全部匹配。"""
        if (consent['user_id']!=user or consent['status']!='active' or consent['expires_at']<=self.now()
            or consent['installation_version']!=installation['version']):
            raise PermissionDeniedError('员工连接需要重新授权')

    def reference(self,state: ConnectionState,cid: str,user: str,operation: str,
                  ref: ConnectionRef | None=None) -> tuple[dict[str,Any],ConnectionRef]:
        """已有后台引用不能默默升级到新的consent。"""
        install=get(state,cid,'installation')
        self.active(install)
        candidates=[c for c in state.records.values() if c.get('kind')=='consent'
                    and c['installation_id']==cid and c['user_id']==user and operation in c['operations']]
        if ref is not None:
            candidates=[c for c in candidates if c['consent_id']==ref.consent_id and c['epoch']==ref.consent_epoch]
        for consent in reversed(candidates):
            try:
                self.valid_consent(consent,install,user)
            except PermissionDeniedError:
                continue
            current=ConnectionRef(installation_id=cid,consent_id=consent['consent_id'],
                                  consent_epoch=consent['epoch'],installation_version=install['version'])
            if ref is not None and ref!=current:
                continue
            return install,current
        raise PermissionDeniedError('员工连接需要重新授权')
