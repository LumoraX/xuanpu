"""能力命令通过公开SDK操作，凭据与确认正文仅从显式文件读取。"""
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


def add_capability_commands(commands: Any) -> None:
    """列目录、登记、预检和安装不提供自动激活选项。"""
    parent = commands.add_parser('capabilities', help='能力目录、安装预检及停止配置')
    parent.add_argument('--credentials', required=True, help='仅当前用户可读的私有凭据JSON')
    operations = parent.add_subparsers(dest='operation', required=True)
    for name in ('list', 'mine', 'register', 'install-preview', 'install', 'state'):
        parser = operations.add_parser(name)
        if name in ('install-preview', 'install', 'state'):
            parser.add_argument('identifier')
        if name in ('register', 'install-preview', 'install', 'state'):
            parser.add_argument('--body', required=True, help='含原操作号及明确确认的JSON，不自动生成确认')
        if name == 'list':
            parser.add_argument('--limit', type=int, default=50)
            parser.add_argument('--cursor')
        parser.set_defaults(handler=cmd_capabilities)


async def run_capabilities(args: argparse.Namespace) -> dict[str, Any]:
    """不允许请求正文覆盖认证身份，断连重试复用同一输入文件。"""
    credentials_path = Path(args.credentials)
    if credentials_path.stat().st_mode & 0o077:
        raise ValueError('凭据文件必须仅当前用户可读')
    credentials = await asyncio.to_thread(read_object, args.credentials)
    if set(credentials) - {'base_url', 'api_key', 'act_as_user'}:
        raise ValueError('凭据字段无效')
    body = await asyncio.to_thread(read_object, args.body) if getattr(args, 'body', None) else {}
    if {'tenant_id', 'actor', 'user_id', 'api_key', 'act_as_user', 'host_path'} & body.keys():
        raise ValueError('请求不能覆盖调用身份')
    async with XuanpuClient(**credentials) as client:
        if args.operation == 'list':
            return await client.list_capabilities(limit=args.limit, cursor=args.cursor)
        if args.operation == 'mine':
            return await client.my_capabilities()
        if args.operation == 'register':
            return await client.register_capability(body)
        if args.operation == 'install-preview':
            return await client.preview_capability_install(args.identifier, body)
        if args.operation == 'install':
            return await client.install_capability(args.identifier, body)
        if args.operation == 'state':
            return await client.change_capability_installation(args.identifier, body)
    raise ValueError('未知能力操作')


def cmd_capabilities(args: argparse.Namespace) -> int:
    """错误不回显凭据或提交正文；成功仅显示服务端授权视图。"""
    try:
        result = asyncio.run(run_capabilities(args))
    except (OSError, ValueError, TypeError, KeyError, XuanpuError, XuanpuTransportError):
        print('操作未完成，请检查私有凭据、能力权限及预检版本；重试保留原操作号。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
