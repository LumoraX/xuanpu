"""到人共享命令使用固定员工档案和原确认文件，不提供身份覆盖。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_sdk import XuanpuClient, XuanpuError, XuanpuTransportError

from xuanpu_cli.work_commands import read_object

if TYPE_CHECKING:
    import argparse


def add_shared_commands(commands: Any) -> None:
    """共享事项、成员、固定材料和阅读都有无模型命令。"""
    parent = commands.add_parser('shared', help='本人共享事项、固定材料及受保护阅读')
    parent.add_argument('--credentials', required=True, help='权限600的私有员工凭据档案')
    subs = parent.add_subparsers(dest='operation', required=True)
    operations = ('list', 'get', 'create', 'change', 'members', 'invite', 'revoke', 'transfer',
        'events', 'materials', 'publish', 'publish-text', 'publication', 'upload', 'download', 'withdraw',
        'context-list', 'context-create', 'context-get', 'context-read', 'context-close', 'context-ask',
        'notifications', 'read-notification')
    for operation in operations:
        command = subs.add_parser(operation)
        if operation not in ('list', 'create', 'context-list', 'context-create', 'notifications'):
            command.add_argument('identifier')
        if operation in ('create', 'change', 'invite', 'revoke', 'transfer', 'publish', 'publish-text',
                         'upload', 'withdraw', 'context-create', 'context-close', 'context-ask', 'read-notification'):
            command.add_argument('--body', required=True, help='含原request_id、版本和明确确认的JSON文件')
        if operation in ('list', 'members', 'events', 'materials', 'context-list', 'notifications'):
            command.add_argument('--limit', type=int, default=50)
            command.add_argument('--cursor')
        if operation == 'list':
            command.add_argument('--search', default='')
        _extra_arguments(command, operation)
        command.set_defaults(handler=cmd_shared)


def _extra_arguments(command: Any, operation: str) -> None:
    """材料及通知附加参数与通用JSON确认参数分开，保持命令声明有界。"""
    if operation == 'notifications':
        command.add_argument('--after-id', type=int, default=0)
    if operation == 'revoke':
        command.add_argument('--member', required=True)
    if operation in ('download', 'withdraw'):
        command.add_argument('--material', required=True)
    if operation == 'publication':
        command.add_argument('--publication', required=True)
    if operation == 'download':
        command.add_argument('--revision', type=int, required=True)
        command.add_argument('--output', required=True, help='明确的新导出路径，不覆盖现有文件')
    if operation == 'upload':
        command.add_argument('--file', required=True)


def write_export(path: str, data: bytes) -> dict[str, Any]:
    """只在SDK已核验后创建本人可读的新文件，拒绝覆盖已有内容。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output:
        output.write(data)
    return {'output': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def read_upload(path: str) -> bytes:
    """输入文件有界读取，不通过JSON或服务端主机路径上传字节。"""
    with Path(path).open('rb') as source:
        data = source.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('共享文件超过20MB')
    return data


async def run_shared(args: argparse.Namespace) -> dict[str, Any]:
    """固定私有档案中的身份；断线重试沿用原正文，不自动补确认。"""
    if Path(args.credentials).stat().st_mode & 0o077:
        raise ValueError('私有档案必须仅当前用户可读')
    credentials = await asyncio.to_thread(read_object, args.credentials)
    if set(credentials) - {'base_url', 'api_key', 'act_as_user'}:
        raise ValueError('凭据档案字段无效')
    body = await asyncio.to_thread(read_object, args.body) if getattr(args, 'body', None) else {}
    if {'tenant_id', 'api_key', 'act_as_user', 'host_path'} & body.keys():
        raise ValueError('请求不能覆盖调用身份')
    async with XuanpuClient(**credentials) as client:
        if args.operation == 'download':
            data = await client.download_shared_material(args.identifier, args.material, revision=args.revision)
            return await asyncio.to_thread(write_export, args.output, data)
        if args.operation == 'upload':
            data = await asyncio.to_thread(read_upload, args.file)
            return await client.upload_shared_material(args.identifier, data, **body)
        return await _dispatch(client, args, body)


async def _dispatch(client: XuanpuClient, args: argparse.Namespace, body: dict[str, Any]) -> dict[str, Any]:
    """只选择一个公开SDK操作，用户目标编号始终由服务器检查资格。"""
    identifier = getattr(args, 'identifier', '')
    limit, cursor = getattr(args, 'limit', 50), getattr(args, 'cursor', None)
    calls = {
        'list': lambda: client.list_shared_matters(search=args.search, limit=limit, cursor=cursor),
        'get': lambda: client.get_shared_matter(identifier),
        'create': lambda: client.create_shared_matter(body),
        'change': lambda: client.change_shared_matter(identifier, body),
        'members': lambda: client.list_shared_members(identifier, limit=limit, cursor=cursor),
        'invite': lambda: client.invite_shared_member(identifier, body),
        'revoke': lambda: client.revoke_shared_member(identifier, args.member, body),
        'transfer': lambda: client.transfer_shared_owner(identifier, body),
        'events': lambda: client.shared_matter_events(identifier, limit=limit, cursor=cursor),
        'materials': lambda: client.list_shared_materials(identifier, limit=limit, cursor=cursor),
        'publish': lambda: client.publish_shared_material(identifier, body),
        'publish-text': lambda: client.publish_shared_text(identifier, body),
        'publication': lambda: client.shared_publication(identifier, args.publication),
        'withdraw': lambda: client.withdraw_shared_material(identifier, args.material, body),
        'context-create': lambda: client.create_shared_context(body),
        'context-list': lambda: client.list_shared_contexts(limit=limit, cursor=cursor),
        'context-get': lambda: client.get_shared_context(identifier),
        'context-read': lambda: client.read_shared_context(identifier),
        'context-close': lambda: client.close_shared_context(identifier, body),
        'context-ask': lambda: client.shared_source_reply(identifier, **body),
        'notifications': lambda: client.shared_notifications(after_id=args.after_id, limit=limit),
        'read-notification': lambda: client.read_shared_notification(identifier, body),
    }
    return await calls[args.operation]()


def cmd_shared(args: argparse.Namespace) -> int:
    """错误不回显凭据和私有正文，输出只来自当前授权的产品接口。"""
    try:
        result = asyncio.run(run_shared(args))
    except (OSError, ValueError, TypeError, KeyError, XuanpuError, XuanpuTransportError):
        print('共享操作未完成，请检查本人权限、私有档案和固定版本；重试使用原操作号。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
