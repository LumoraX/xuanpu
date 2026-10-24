"""协作命令使用固定私有员工档案和明确确认文件，冲突不自动推进业务。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xuanpu_sdk import XuanpuClient, XuanpuError, XuanpuTransportError
from xuanpu_sdk.collaboration import HANDOFF_ACTIONS

from xuanpu_cli.shared_commands import read_upload
from xuanpu_cli.work_commands import read_object

if TYPE_CHECKING:
    import argparse


def add_collaboration_commands(commands: Any) -> None:
    """到人交接、固定成果、组织审批及通知有无模型命令。"""
    parser=commands.add_parser('collaboration',help='本人工作交接与组织审批')
    parser.add_argument('--credentials',required=True,help='权限600的私有员工凭据档案')
    subs=parser.add_subparsers(dest='operation',required=True)
    operations=(*sorted(HANDOFF_ACTIONS),'list','get','submit','result-upload','result-text','result-publication',
        'policies','policy-create','policy-change','approval-list','approval-get','approval-submit',
        'approve','reject','approval-return','approval-withdraw','notifications','read-notification')
    for operation in operations:
        command=subs.add_parser(operation)
        if operation not in ('list','submit','policies','policy-create','approval-list','approval-submit','notifications'):
            command.add_argument('identifier')
        if operation in HANDOFF_ACTIONS or operation in ('submit','result-upload','result-text','policy-create','policy-change',
                'approval-submit','approve','reject','approval-return','approval-withdraw','read-notification'):
            command.add_argument('--body',required=True,help='权限600的原操作号、确认与固定版本JSON文件')
        if operation in ('list','policies','approval-list','notifications'):
            command.add_argument('--limit',type=int,default=50);command.add_argument('--cursor')
        if operation=='list':
            command.add_argument('--view',choices=('received','sent','assigned','department_queue'),default='received')
        if operation=='policies':
            command.add_argument('--admin',action='store_true')
        if operation=='result-upload':
            command.add_argument('--file',required=True)
        if operation=='result-publication':
            command.add_argument('--publication',required=True)
        if operation=='notifications':
            command.add_argument('--after-id',type=int,default=0)
        command.set_defaults(handler=cmd_collaboration)


async def run_collaboration(args: argparse.Namespace) -> dict[str,Any]:
    """命令凭据只来自当前指定档案，正文不能覆盖身份、主机路径或凭据。"""
    for path in (args.credentials,getattr(args,'body',None)):
        if path and Path(path).stat().st_mode & 0o077:
            raise ValueError('私有档案必须仅本人可读')
    credentials=await asyncio.to_thread(read_object,args.credentials)
    if set(credentials)-{'base_url','api_key','act_as_user'}:
        raise ValueError('凭据档案字段无效')
    body=await asyncio.to_thread(read_object,args.body) if getattr(args,'body',None) else {}
    if {'tenant_id','actor_id','api_key','act_as_user','host_path'} & body.keys():
        raise ValueError('不能覆盖调用身份或主机路径')
    async with XuanpuClient(**credentials) as client:
        return await dispatch(client,args,body)


async def dispatch(client: XuanpuClient,args: argparse.Namespace,body: dict[str,Any]) -> dict[str,Any]:
    """命令始终原样提交确认，任何重试仍由用户保留同一操作号。"""
    identifier=getattr(args,'identifier','');limit=getattr(args,'limit',50);cursor=getattr(args,'cursor',None)
    if args.operation in HANDOFF_ACTIONS:
        return await client.handoff_action(identifier,args.operation,body)
    if args.operation in ('approve','reject','approval-return','approval-withdraw'):
        return await client.approval_decision(identifier,args.operation.removeprefix('approval-'),body)
    if args.operation=='result-upload':
        data=await asyncio.to_thread(read_upload,args.file)
        return await client.upload_handoff_result(identifier,data,**body)
    calls={
        'list':lambda:client.list_handoffs(view=args.view,limit=limit,cursor=cursor),
        'get':lambda:client.get_handoff(identifier),'submit':lambda:client.submit_handoff(body),
        'result-text':lambda:client.publish_handoff_text(identifier,body),
        'result-publication':lambda:client.handoff_publication(identifier,args.publication),
        'policies':lambda:client.list_approval_policies(admin=args.admin,limit=limit,cursor=cursor),
        'policy-create':lambda:client.create_approval_policy(body),'policy-change':lambda:client.change_approval_policy(identifier,body),
        'approval-list':lambda:client.list_approval_requests(limit=limit,cursor=cursor),
        'approval-get':lambda:client.get_approval_request(identifier),'approval-submit':lambda:client.submit_approval_request(body),
        'notifications':lambda:client.collaboration_notifications(after_id=args.after_id,limit=limit),
        'read-notification':lambda:client.read_collaboration_notification(identifier,body),
    }
    return await calls[args.operation]()


def cmd_collaboration(args: argparse.Namespace) -> int:
    """失败不回显凭据或私有正文；成功仅输出本人当前有权取得的产品数据。"""
    try:
        result=asyncio.run(run_collaboration(args))
    except (OSError,ValueError,TypeError,KeyError,XuanpuError,XuanpuTransportError):
        print('协作操作未完成，请核对本人权限、私有档案及固定版本；重试保留原操作号。',file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0
