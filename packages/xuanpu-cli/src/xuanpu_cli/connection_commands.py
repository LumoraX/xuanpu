"""本人连接命令，身份仅来自受限私有文件，不接受命令行覆盖。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_sdk import XuanpuClient, XuanpuError, XuanpuTransportError

if TYPE_CHECKING:
    import argparse


def add_connection_commands(commands: Any) -> None:
    """同意仅给出浏览器入口，read/revoke不扩大授权。"""
    parent=commands.add_parser('connections',help='本人的企业只读连接')
    parent.add_argument('--credentials',required=True,help='权限600的私有JSON凭据档案')
    subs=parent.add_subparsers(dest='operation',required=True)
    for operation in ('list','read','revoke','confirm'):
        sub=subs.add_parser(operation)
        if operation in ('read','revoke'):
            sub.add_argument('identifier')
        if operation=='revoke':
            sub.add_argument('consent_id')
            sub.add_argument('--request-id',required=True)
            sub.add_argument('--expected-version',required=True,type=int)
        sub.set_defaults(handler=cmd_connection)


async def run_connection(args: argparse.Namespace) -> dict[str,Any]:
    """凭据只读，不打印档案或自动申请授权。"""
    path=Path(args.credentials)
    if path.stat().st_mode&0o077:
        raise ValueError('私有档案必须仅当前用户可读')
    value=await asyncio.to_thread(path.read_text)
    credentials=json.loads(value)
    if not isinstance(credentials,dict) or set(credentials)-{'base_url','api_key','act_as_user'}:
        raise ValueError('凭据档案字段无效')
    async with XuanpuClient(**credentials) as client:
        if args.operation=='list':
            return await client.list_connections()
        if args.operation=='read':
            return await client.read_connection(args.identifier)
        if args.operation=='revoke':
            return await client.revoke_connection_consent(args.identifier,args.consent_id,
                request_id=args.request_id,expected_version=args.expected_version)
        return {'confirmation_url':client.connection_confirmation_url(),
                'message':'请员工本人登录浏览器后确认；此命令未创建授权'}


def cmd_connection(args: argparse.Namespace) -> int:
    """失败仅输出安全分类，成功仅输出已投影的本人数据。"""
    try:
        result=asyncio.run(run_connection(args))
    except (OSError,ValueError,TypeError,KeyError,XuanpuError,XuanpuTransportError):
        print('操作未完成，请检查私有档案、员工身份及本人连接状态。',file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0
