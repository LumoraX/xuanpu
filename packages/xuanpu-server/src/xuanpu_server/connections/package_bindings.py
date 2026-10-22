"""核定包摘要与固定只读操作的不可变绑定。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from xuanpu_spec import load_openapi

from xuanpu_server.connections.rules import audit, fingerprint, get
from xuanpu_server.errors import ConflictError, NotFoundError

if TYPE_CHECKING:
    from xuanpu_server.connections.models import PackageBindingInput
    from xuanpu_server.connections.service import ConnectionService
    from xuanpu_server.tenancy import Principal


def binding_key(digest: str,connector: str,operation: str) -> str:
    """稳定键不能由另一安装重绑，同摘要即同一版本内容。"""
    return 'package-binding:'+fingerprint([digest,connector,operation])


async def bind(service: ConnectionService,who: Principal,cid: str,
               body: PackageBindingInput) -> dict[str,Any]:
    """管理员核定固定目的端与受保护参数，重复记录不能改写。"""
    tenant,_=await service.actor(who,admin=True)
    expected=('/cgi-bin/user/get',{'userid','access_token'}) if body.operation=='self_profile.read' else ('/cgi-bin/department/list',{'access_token'})
    if body.endpoint!=expected[0] or set(body.server_bound_parameters)!=expected[1]:
        raise ValueError('只读操作与endpoint/受保护参数不一致')
    records=await service.control.list_package_versions(tenant)
    published=next((r for r in records if r.digest.removeprefix('sha256:')==body.package_digest),None)
    if published is None or service.packages is None:
        raise NotFoundError('不可变业务包尚未发布或校验')
    package=await service.packages.resolve(tenant,published.name,published.version)
    spec=next((c for c in package.manifest.spec.connectors if c.name==body.connector_name),None)
    if spec is None or spec.transport!='http' or spec.openapi is None:
        raise ValueError('仅核定已发布的HTTP只读连接器')
    document=await asyncio.to_thread(load_openapi,package.root/spec.openapi)
    operation=document.operations.get(body.operation_id)
    if operation is None:
        raise ValueError('操作不在已发布文档中')
    from xuanpu_server.connections.managed_http import ManagedHttpTools
    from xuanpu_server.runtime.http_api import resolve_base_url
    ManagedHttpTools.validate(body.model_dump(),spec,operation,resolve_base_url(spec,document))
    key=binding_key(body.package_digest,body.connector_name,body.operation_id)
    result={'kind':'package_binding','id':key,'installation_id':cid,
            **body.model_dump(exclude={'request_id'})}
    async with service.store.transaction(tenant) as state:
        get(state,cid,'installation')
        old=state.records.get(key)
        if old is not None and old!=result:
            raise ConflictError('不可变包操作已绑定；更换安装需发布新包版本')
        state.records[key]=result
        audit(state,who,'package_bound',service.now(),installation_id=cid,binding_id=key)
    return {'id':key,'installation_id':cid,'package_digest':body.package_digest,
            'operation_id':body.operation_id,'operation':body.operation,'effect':'read'}
