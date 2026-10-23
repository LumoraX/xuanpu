"""组织命令只调用公开SDK；确认正文及操作号由私有输入文件提供。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_sdk import XuanpuClient, XuanpuError, XuanpuTransportError

from xuanpu_cli.work_commands import read_object

if TYPE_CHECKING:
    import argparse

    from xuanpu_sdk.organization import Collection


def add_organization_commands(commands: Any) -> None:
    """组织配置、目录和显式来源确认都有对应命令。"""
    parent = commands.add_parser('organization', help='组织岗位、职责及目录预览')
    parent.add_argument('--credentials', required=True, help='权限600的私有JSON凭据档案')
    subs = parent.add_subparsers(dest='operation', required=True)
    for operation in ('me', 'directory', 'list', 'get', 'create', 'change', 'authority',
                      'preview-authority', 'apply-authority', 'preview-import', 'get-import', 'apply-import'):
        command = subs.add_parser(operation)
        if operation in ('list', 'get', 'create', 'change'):
            command.add_argument('collection', choices=('units', 'positions', 'memberships', 'roles'))
        if operation in ('get', 'change', 'get-import', 'apply-import', 'apply-authority'):
            command.add_argument('identifier')
        if operation in ('create', 'change', 'preview-authority', 'apply-authority', 'preview-import', 'apply-import'):
            command.add_argument('--body', required=True, help='含原request_id、版本及明确选择的JSON文件')
        if operation in ('list', 'directory'):
            command.add_argument('--limit', type=int, default=50)
            command.add_argument('--cursor')
        command.set_defaults(handler=cmd_organization)


async def run_organization(args: argparse.Namespace) -> dict[str, Any]:
    """档案限制身份；user_id仅能作为需要服务端授权的目标员工字段。"""
    path = Path(args.credentials)
    if path.stat().st_mode & 0o077:
        raise ValueError('私有档案必须仅当前用户可读')
    credentials = await asyncio.to_thread(read_object, args.credentials)
    if set(credentials) - {'base_url', 'api_key', 'act_as_user'}:
        raise ValueError('凭据档案字段无效')
    body = await asyncio.to_thread(read_object, args.body) if getattr(args, 'body', None) else {}
    if {'tenant_id', 'api_key', 'act_as_user', 'host_path'} & body.keys():
        raise ValueError('请求不能覆盖调用身份')
    async with XuanpuClient(**credentials) as client:
        return await _dispatch(client, args, body)


async def _dispatch(client: XuanpuClient, args: argparse.Namespace, body: dict[str, Any]) -> dict[str, Any]:
    """单次调用不自动重试新操作，断线后复用输入文件。"""
    operation = args.operation
    collection: Collection = getattr(args, 'collection', 'units')
    identifier = getattr(args, 'identifier', '')
    if operation == 'me':
        return await client.organization_me()
    if operation == 'directory':
        return await client.organization_directory(limit=args.limit, cursor=args.cursor)
    if operation == 'list':
        return await client.list_organization(collection, limit=args.limit, cursor=args.cursor)
    if operation == 'get':
        return await client.get_organization(collection, identifier)
    if operation == 'create':
        return await client.create_organization(collection, body)
    if operation == 'change':
        return await client.change_organization(collection, identifier, body)
    if operation == 'authority':
        return await client.organization_authority()
    if operation == 'preview-authority':
        return await client.preview_organization_authority(body)
    if operation == 'apply-authority':
        return await client.apply_organization_authority(identifier, body)
    if operation == 'preview-import':
        return await client.preview_organization_import(body)
    if operation == 'get-import':
        return await client.get_organization_import(identifier)
    if operation == 'apply-import':
        return await client.apply_organization_import(identifier, body)
    raise ValueError('未知组织操作')


def cmd_organization(args: argparse.Namespace) -> int:
    """失败不回显档案及供应商正文，成功只输出已授权的领域视图。"""
    try:
        result = asyncio.run(run_organization(args))
    except (OSError, ValueError, TypeError, KeyError, XuanpuError, XuanpuTransportError):
        print('操作未完成，请检查私有档案、组织权限和预览版本；重试使用原操作号。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
