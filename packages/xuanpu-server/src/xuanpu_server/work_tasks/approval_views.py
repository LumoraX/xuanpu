"""从已发布目录提供知情批准说明，不转发模型参数或宿主路径。"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from xuanpu_spec import OpenApiError, load_openapi

from xuanpu_server.errors import NotFoundError, PackageInvalidError, RuntimeUnavailableError

if TYPE_CHECKING:
    from .models import WorkTaskRecord

LABELS={'shell_exec':'在隔离空间执行材料处理命令','script_exec':'运行已发布的材料处理脚本',
        'file_read':'读取本次交办的材料','file_write':'在隔离空间保存处理结果'}


async def approval_labels(packages: Any,task: WorkTaskRecord) -> dict[str,dict[str,Any]]:
    """HTTP说明只取固定版本OpenAPI；取不到说明的业务调用不能在简略卡片批准。"""
    try:
        return await _resolve_labels(packages,task)
    except (RuntimeUnavailableError,NotFoundError,PackageInvalidError,OpenApiError,OSError):
        return {p['request_id']:{'summary':LABELS.get(p.get('detail',{}).get('scope'),
            '操作说明暂时无法核实，可以拒绝或停止工作。'),
            'can_approve':p.get('detail',{}).get('scope') in LABELS} for p in task.pending}


async def _resolve_labels(packages: Any,task: WorkTaskRecord) -> dict[str,dict[str,Any]]:
    """固定能力目录失败上交外层，只降级说明，不改变员工内容权限。"""
    labels={}
    package=None
    for pending in task.pending:
        scope=pending.get('detail',{}).get('scope')
        identifier=pending['request_id']
        if scope in LABELS:
            labels[identifier]={'summary':LABELS[scope],'can_approve':True}
            continue
        target=pending.get('detail',{}).get('target','')
        if not isinstance(target,str) or not target.startswith('api__'):
            labels[identifier]={'summary':'业务操作说明尚未核实，请查看原业务记录后决定。','can_approve':False}
            continue
        if package is None:package=await packages.resolve(task.tenant_id,task.package,task.package_version)
        for connector in package.manifest.spec.connectors:
            prefix='api__'+connector.name+'__'
            if connector.transport!='http' or not connector.openapi or not target.startswith(prefix):continue
            document=await asyncio.to_thread(load_openapi,package.root/connector.openapi)
            operation=document.operations.get(target.removeprefix(prefix))
            if operation is not None and connector.allows(target.removeprefix(prefix)):
                effect=connector.effect_of(target.removeprefix(prefix),read_only=operation.read_only)
                labels[identifier]={'summary':operation.summary or ('读取业务记录' if effect=='read' else '变更业务记录'),
                    'effect':effect,'can_approve':bool(operation.summary)}
        if identifier not in labels:labels[identifier]={'summary':'原业务操作已无法核实，请拒绝或停止工作。','can_approve':False}
    return labels
